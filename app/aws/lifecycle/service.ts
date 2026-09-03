import {
  DeleteCommand,
  ScanCommand,
  TransactWriteCommand,
  UpdateCommand,
  type ScanCommandOutput,
} from "@aws-sdk/lib-dynamodb";
import { unmarshall } from "@aws-sdk/util-dynamodb";

import type { DynamoClient, MicrovmService } from "../dependencies.js";
import { errorMessage, errorName, errorProperty } from "../errors.js";
import {
  databaseRecord,
  instanceRecord,
  poolConfigRecord,
} from "../records.js";
import type {
  BatchItemFailureResponse,
  DatabaseRecord,
  DynamoStreamRecord,
  InstanceRecord,
  LeaseRecord,
  LifecycleEvent,
  ReconcileSummary,
} from "../types.js";

type LifecycleMicrovms = Pick<MicrovmService, "get" | "list" | "terminate">;

export interface LifecycleServiceOptions {
  readonly ddb: DynamoClient;
  readonly microvms: LifecycleMicrovms;
  readonly tableName: string;
  readonly executionRoleArn: string;
  readonly orphanGraceSeconds: number;
  readonly clock?: () => number;
}

export interface LifecycleService {
  readonly handler: (
    event: LifecycleEvent,
  ) => Promise<BatchItemFailureResponse | ReconcileSummary>;
  readonly processStream: (
    records: readonly DynamoStreamRecord[],
  ) => Promise<BatchItemFailureResponse>;
  readonly reconcile: () => Promise<ReconcileSummary>;
}

function cancellationCode(reason: unknown): string | undefined {
  if (typeof reason !== "object" || reason === null) return undefined;
  const code = Reflect.get(reason, "Code");
  return typeof code === "string" ? code : undefined;
}

function isConditionalCancellation(error: unknown): boolean {
  if (errorName(error) === "ConditionalCheckFailedException") return true;
  if (errorName(error) !== "TransactionCanceledException") return false;
  const candidate = errorProperty(error, "CancellationReasons")
    ?? errorProperty(error, "cancellationReasons");
  const reasons = Array.isArray(candidate) ? candidate : [];
  if (reasons.length > 0) {
    return reasons.some((reason) => cancellationCode(reason) === "ConditionalCheckFailed")
      && reasons.every((reason) => (
        cancellationCode(reason) === undefined
        || cancellationCode(reason) === "None"
        || cancellationCode(reason) === "ConditionalCheckFailed"
      ));
  }
  return /conditional/i.test(errorMessage(error));
}

export function isTtlServiceRemoval(record: DynamoStreamRecord): boolean {
  return record.eventName === "REMOVE"
    && record.userIdentity?.type === "Service"
    && record.userIdentity?.principalId === "dynamodb.amazonaws.com";
}

function stringField(record: DatabaseRecord, key: string): string | undefined {
  const value = record[key];
  return typeof value === "string" ? value : undefined;
}

function numberField(record: DatabaseRecord, key: string): number | undefined {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function leaseRecord(record: DatabaseRecord): LeaseRecord {
  const requestId = stringField(record, "request_id");
  const microvmId = stringField(record, "microvm_id");
  const leaseExpiresAt = numberField(record, "lease_expires_at");
  const ttlAt = numberField(record, "ttl_at");
  if (record.entity !== "LEASE" || !requestId || !microvmId
    || leaseExpiresAt === undefined || ttlAt === undefined) {
    throw new Error("Invalid DynamoDB lease record");
  }
  return {
    ...record,
    entity: "LEASE",
    request_id: requestId,
    microvm_id: microvmId,
    lease_expires_at: leaseExpiresAt,
    ttl_at: ttlAt,
  };
}

function recordIdentifier(record: DynamoStreamRecord): string {
  return record.dynamodb?.SequenceNumber ?? record.eventID ?? "unknown";
}

export function createLifecycleService({
  ddb,
  microvms,
  tableName,
  executionRoleArn,
  orphanGraceSeconds,
  clock = () => Math.floor(Date.now() / 1000),
}: LifecycleServiceOptions): LifecycleService {
  function isNotFound(error: unknown): boolean {
    return errorName(error) === "ResourceNotFoundException";
  }

  async function terminateSafely(microvmId: string | undefined): Promise<void> {
    if (!microvmId) return;
    try {
      await microvms.terminate(microvmId);
    } catch (error) {
      if (!isNotFound(error)) throw error;
    }
  }

  async function markAvailableWhenIdle(instanceKey: {
    readonly pool_key: string;
    readonly record_key: string;
  }): Promise<void> {
    await ddb.send(new UpdateCommand({
      TableName: tableName,
      Key: instanceKey,
      ConditionExpression: "#state = :ready AND in_flight = :zero",
      UpdateExpression: "SET #status = :available",
      ExpressionAttributeNames: { "#state": "state", "#status": "status" },
      ExpressionAttributeValues: {
        ":ready": "READY",
        ":zero": 0,
        ":available": "AVAILABLE",
      },
    })).catch((error: unknown) => {
      if (!isConditionalCancellation(error)) throw error;
    });
  }

  async function recoverDeletedLease(
    lease: LeaseRecord,
    eventId: string,
    now: number,
  ): Promise<void> {
    const instanceKey = {
      pool_key: lease.pool_key,
      record_key: `INSTANCE#${lease.microvm_id}`,
    };
    try {
      // DynamoDB Streams are at-least-once. The recovery marker makes the
      // decrement idempotent even though the TTL service already removed the
      // lease row itself.
      await ddb.send(new TransactWriteCommand({
        TransactItems: [
          {
            Put: {
              TableName: tableName,
              Item: {
                pool_key: lease.pool_key,
                record_key: `RECOVERY#${eventId}`,
                entity: "RECOVERY",
                recovered_lease: lease.record_key,
                ttl_at: now + 86_400,
              },
              ConditionExpression: "attribute_not_exists(pool_key)",
            },
          },
          {
            Update: {
              TableName: tableName,
              Key: instanceKey,
              ConditionExpression: "microvm_id = :microvm AND in_flight > :zero",
              UpdateExpression: "ADD in_flight :minus_one",
              ExpressionAttributeValues: {
                ":microvm": lease.microvm_id,
                ":zero": 0,
                ":minus_one": -1,
              },
            },
          },
        ],
      }));
    } catch (error) {
      if (!isConditionalCancellation(error)) throw error;
    }
    // If a prior delivery committed the transaction but failed before this
    // update, its recovery marker makes the retry conditional. Re-checking the
    // derived status here repairs that at-least-once delivery edge safely.
    await markAvailableWhenIdle(instanceKey);
  }

  async function processStream(
    records: readonly DynamoStreamRecord[],
  ): Promise<BatchItemFailureResponse> {
    const failures: { itemIdentifier: string }[] = [];
    await Promise.all(records.map(async (record) => {
      // The event-source mapping applies this same filter. Keep the ownership
      // check in the handler too: ordinary lease deletes must never be treated
      // as TTL expiry or they would decrement in_flight a second time.
      if (!isTtlServiceRemoval(record) || !record.dynamodb?.OldImage) return;
      try {
        const old = databaseRecord(unmarshall(record.dynamodb.OldImage));
        const microvmId = stringField(old, "microvm_id");
        if (old.entity === "INSTANCE" && microvmId) {
          await terminateSafely(microvmId);
          console.info(JSON.stringify({
            event: "ttl_instance_terminated",
            microvm_id: microvmId,
            pool_key: old.pool_key,
          }));
        } else if (old.entity === "LEASE" && microvmId) {
          await recoverDeletedLease(leaseRecord(old), recordIdentifier(record), clock());
        } else if (old.entity === "RESERVATION" && microvmId) {
          await terminateSafely(microvmId);
        }
      } catch (error) {
        console.error(JSON.stringify({
          event: "ttl_termination_failed",
          error: errorMessage(error),
        }));
        failures.push({ itemIdentifier: recordIdentifier(record) });
      }
    }));
    return { batchItemFailures: failures };
  }

  async function scanState(): Promise<DatabaseRecord[]> {
    const items: DatabaseRecord[] = [];
    let exclusiveStartKey: Record<string, unknown> | undefined;
    do {
      const page = await ddb.send(new ScanCommand({
        TableName: tableName,
        ConsistentRead: true,
        ExclusiveStartKey: exclusiveStartKey,
      })) as ScanCommandOutput;
      items.push(...(page.Items ?? []).map(databaseRecord));
      exclusiveStartKey = page.LastEvaluatedKey;
    } while (exclusiveStartKey);
    return items;
  }

  async function deleteItem(item: DatabaseRecord): Promise<void> {
    await ddb.send(new DeleteCommand({
      TableName: tableName,
      Key: { pool_key: item.pool_key, record_key: item.record_key },
    }));
  }

  async function recoverLease(lease: LeaseRecord, now: number): Promise<void> {
    const instanceKey = {
      pool_key: lease.pool_key,
      record_key: `INSTANCE#${lease.microvm_id}`,
    };
    try {
      await ddb.send(new TransactWriteCommand({
        TransactItems: [
          {
            Delete: {
              TableName: tableName,
              Key: { pool_key: lease.pool_key, record_key: lease.record_key },
              ConditionExpression: "lease_expires_at <= :now",
              ExpressionAttributeValues: { ":now": now },
            },
          },
          {
            Update: {
              TableName: tableName,
              Key: instanceKey,
              ConditionExpression: "in_flight > :zero",
              UpdateExpression: "ADD in_flight :minus_one",
              ExpressionAttributeValues: { ":zero": 0, ":minus_one": -1 },
            },
          },
        ],
      }));
      await markAvailableWhenIdle(instanceKey);
      console.warn(JSON.stringify({
        event: "expired_lease_recovered",
        microvm_id: lease.microvm_id,
        request_id: lease.request_id,
      }));
    } catch (error) {
      if (!isConditionalCancellation(error)) throw error;
      await deleteItem(lease);
    }
  }

  function epochSeconds(value: Date | string | undefined): number | undefined {
    if (!value) return undefined;
    const milliseconds = new Date(value).getTime();
    return Number.isFinite(milliseconds) ? Math.floor(milliseconds / 1000) : undefined;
  }

  async function reconcileOrphans(items: readonly DatabaseRecord[], now: number): Promise<number> {
    const knownIds = new Set(
      items
        .filter((item) => (
          (item.entity === "INSTANCE" || item.entity === "RESERVATION")
          && typeof item.microvm_id === "string"
        ))
        .map((item) => item.microvm_id as string),
    );
    const configs = items
      .filter((item) => item.entity === "POOL")
      .map(poolConfigRecord);
    const uniqueImages = new Map(configs.map((config) => [
      `${config.image_arn}:${config.image_version}`,
      config,
    ]));
    let terminated = 0;

    for (const config of uniqueImages.values()) {
      let nextToken: string | undefined;
      do {
        const page = await microvms.list({
          imageIdentifier: config.image_arn,
          imageVersion: config.image_version,
          ...(nextToken ? { nextToken } : {}),
        });
        for (const summary of page.items ?? []) {
          if (knownIds.has(summary.microvmId)
            || summary.state === "TERMINATING"
            || summary.state === "TERMINATED") continue;
          const startedAt = epochSeconds(summary.startedAt);
          if (!startedAt || startedAt > now - orphanGraceSeconds) continue;

          let details;
          try {
            details = await microvms.get(summary.microvmId);
          } catch (error) {
            if (isNotFound(error)) continue;
            throw error;
          }
          // This generated runtime role is the ownership fence. It avoids
          // touching retained benchmark/manual VMs that use the same image.
          if (details.executionRoleArn !== executionRoleArn) continue;
          await terminateSafely(summary.microvmId);
          terminated += 1;
          console.warn(JSON.stringify({
            event: "orphan_microvm_terminated",
            microvm_id: summary.microvmId,
            image_arn: config.image_arn,
            image_version: config.image_version,
          }));
        }
        nextToken = page.nextToken;
      } while (nextToken);
    }
    return terminated;
  }

  async function reconcile(): Promise<ReconcileSummary> {
    const now = clock();
    const items = await scanState();
    const summary: ReconcileSummary = {
      scanned: items.length,
      terminated: 0,
      recoveredLeases: 0,
      removedReservations: 0,
      terminatedOrphans: 0,
      errors: 0,
    };

    for (const item of items) {
      try {
        if (item.entity === "INSTANCE") {
          const instance: InstanceRecord = instanceRecord(item);
          const logicallyExpired = instance.soft_expires_at <= now
            || instance.hard_expires_at <= now;
          if (logicallyExpired || instance.state === "FAILED") {
            await terminateSafely(instance.microvm_id);
            await deleteItem(instance);
            summary.terminated += 1;
          }
        } else if (item.entity === "RESERVATION") {
          const ttlAt = numberField(item, "ttl_at");
          if (ttlAt !== undefined && ttlAt <= now) {
            await terminateSafely(stringField(item, "microvm_id"));
            await deleteItem(item);
            summary.removedReservations += 1;
          }
        } else if (item.entity === "LEASE") {
          const lease = leaseRecord(item);
          if (lease.lease_expires_at <= now) {
            await recoverLease(lease, now);
            summary.recoveredLeases += 1;
          }
        }
      } catch (error) {
        summary.errors += 1;
        console.error(JSON.stringify({
          event: "reconcile_item_failed",
          pool_key: item.pool_key,
          record_key: item.record_key,
          microvm_id: stringField(item, "microvm_id"),
          error: errorMessage(error),
        }));
      }
    }
    try {
      summary.terminatedOrphans = await reconcileOrphans(items, now);
    } catch (error) {
      summary.errors += 1;
      console.error(JSON.stringify({
        event: "orphan_reconcile_failed",
        error: errorMessage(error),
      }));
    }
    console.info(JSON.stringify({ event: "reconcile_complete", ...summary }));
    if (summary.errors > 0) {
      throw new Error(`Reconciliation failed for ${summary.errors} item(s)`);
    }
    return summary;
  }

  async function handler(
    event: LifecycleEvent,
  ): Promise<BatchItemFailureResponse | ReconcileSummary> {
    if ("Records" in event && Array.isArray(event.Records)) {
      return processStream(event.Records);
    }
    return reconcile();
  }

  return { handler, processStream, reconcile };
}
