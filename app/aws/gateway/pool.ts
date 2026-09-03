import { createHash, randomUUID } from "node:crypto";

import {
  DeleteCommand,
  GetCommand,
  PutCommand,
  QueryCommand,
  ScanCommand,
  TransactWriteCommand,
  UpdateCommand,
  type GetCommandOutput,
  type QueryCommandOutput,
  type ScanCommandOutput,
  type TransactWriteCommandInput,
} from "@aws-sdk/lib-dynamodb";

import type { DynamoClient, MicrovmService, PoolRegistryService } from "../dependencies.js";
import { errorMessage, errorName, errorProperty } from "../errors.js";
import {
  databaseRecord,
  isInstanceRecord,
  modelConfig,
  poolConfigRecord,
} from "../records.js";
import type {
  DatabaseRecord,
  InstanceRecord,
  ModelConfig,
  PoolConfigRecord,
  TierConfig,
} from "../types.js";

const READY = "READY" as const;
const AVAILABLE = "AVAILABLE" as const;
const BUSY = "BUSY" as const;

type Sleep = (milliseconds: number) => Promise<void>;

export interface PoolRegistryOptions {
  readonly ddb: DynamoClient;
  readonly microvms: MicrovmService;
  readonly tableName: string;
  readonly clock?: () => number;
  readonly sleep?: Sleep;
  readonly softTtlSeconds?: number;
  readonly hardTtlSeconds?: number;
  readonly leaseSeconds?: number;
  readonly tokenMinutes?: number;
  readonly tokenRefreshMinutes?: number;
  readonly launchLockSeconds?: number;
  readonly hardExpiryHeadroomSeconds?: number;
}

function transactionToken(value: string): string {
  return createHash("sha256").update(value).digest("hex").slice(0, 36);
}

function cancellationCode(reason: unknown): string | undefined {
  if (typeof reason !== "object" || reason === null) return undefined;
  const code = Reflect.get(reason, "Code");
  return typeof code === "string" ? code : undefined;
}

function cancellationReasons(error: unknown): readonly unknown[] {
  const reasons = errorProperty(error, "CancellationReasons")
    ?? errorProperty(error, "cancellationReasons");
  return Array.isArray(reasons) ? reasons : [];
}

function conditionalFailure(error: unknown): boolean {
  if (errorName(error) === "ConditionalCheckFailedException") return true;
  if (errorName(error) !== "TransactionCanceledException") return false;

  const reasons = cancellationReasons(error);
  if (Array.isArray(reasons) && reasons.length > 0) {
    return reasons.some((reason) => cancellationCode(reason) === "ConditionalCheckFailed")
      && reasons.every((reason) => (
        cancellationCode(reason) === undefined
        || cancellationCode(reason) === "None"
        || cancellationCode(reason) === "ConditionalCheckFailed"
      ));
  }
  return /conditional/i.test(errorMessage(error));
}

function retryableTransactionFailure(error: unknown): boolean {
  if ([
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "ThrottlingException",
    "TransactionConflictException",
  ].includes(errorName(error) ?? "")) return true;
  const reasons = cancellationReasons(error);
  return reasons.some((reason) => [
    "ProvisionedThroughputExceeded",
    "ThrottlingError",
    "TransactionConflict",
  ].includes(cancellationCode(reason) ?? ""))
    || /conflict|throttl|throughput/i.test(errorMessage(error));
}

export function modelKey(model: string): string {
  return `MODEL#${model}`;
}

export function poolKey(model: string, tier: string): string {
  return `POOL#${model}#${tier}`;
}

export class PoolRegistry implements PoolRegistryService {
  readonly ddb: DynamoClient;
  readonly microvms: MicrovmService;
  readonly tableName: string;
  readonly clock: () => number;
  readonly sleep: Sleep;
  readonly softTtlSeconds: number;
  readonly hardTtlSeconds: number;
  readonly leaseSeconds: number;
  readonly tokenMinutes: number;
  readonly tokenRefreshMinutes: number;
  readonly launchLockSeconds: number;
  readonly hardExpiryHeadroomSeconds: number;
  private modelCache: { expiresAt: number; models: readonly ModelConfig[] };
  private readonly modelItemCache: Map<string, { expiresAt: number; item: ModelConfig }>;
  private readonly poolConfigCache: Map<string, { expiresAt: number; item: PoolConfigRecord }>;

  constructor({
    ddb,
    microvms,
    tableName,
    clock = () => Math.floor(Date.now() / 1000),
    sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
    softTtlSeconds = 1800,
    hardTtlSeconds = 25200,
    leaseSeconds = 900,
    tokenMinutes = 60,
    tokenRefreshMinutes = Math.max(1, tokenMinutes - 5),
    launchLockSeconds = 180,
    hardExpiryHeadroomSeconds = 900,
  }: PoolRegistryOptions) {
    this.ddb = ddb;
    this.microvms = microvms;
    this.tableName = tableName;
    this.clock = clock;
    this.sleep = sleep;
    this.softTtlSeconds = softTtlSeconds;
    this.hardTtlSeconds = hardTtlSeconds;
    this.leaseSeconds = leaseSeconds;
    this.tokenMinutes = tokenMinutes;
    this.tokenRefreshMinutes = tokenRefreshMinutes;
    this.launchLockSeconds = launchLockSeconds;
    this.hardExpiryHeadroomSeconds = hardExpiryHeadroomSeconds;
    this.modelCache = { expiresAt: 0, models: [] };
    this.modelItemCache = new Map();
    this.poolConfigCache = new Map();
  }

  async listModels(
    { force = false }: { readonly force?: boolean } = {},
  ): Promise<readonly ModelConfig[]> {
    const now = this.clock();
    if (!force && this.modelCache.expiresAt > now) return this.modelCache.models;

    const models: ModelConfig[] = [];
    let ExclusiveStartKey: Record<string, unknown> | undefined;
    do {
      const page = await this.ddb.send(new ScanCommand({
        TableName: this.tableName,
        ConsistentRead: true,
        FilterExpression: "#entity = :model",
        ExpressionAttributeNames: { "#entity": "entity" },
        ExpressionAttributeValues: { ":model": "MODEL" },
        ExclusiveStartKey,
      })) as ScanCommandOutput;
      models.push(...(page.Items ?? []).map(modelConfig));
      ExclusiveStartKey = page.LastEvaluatedKey;
    } while (ExclusiveStartKey);

    models.sort((left, right) => (left.order ?? 0) - (right.order ?? 0));
    this.modelCache = { expiresAt: now + 60, models };
    for (const model of models) {
      this.modelItemCache.set(model.name, { expiresAt: now + 60, item: model });
    }
    return models;
  }

  async getModel(name: string): Promise<ModelConfig | undefined> {
    const cached = this.modelItemCache.get(name);
    const now = this.clock();
    if (cached && cached.expiresAt > now) return cached.item;

    const result = await this.ddb.send(new GetCommand({
      TableName: this.tableName,
      Key: { pool_key: modelKey(name), record_key: "CONFIG" },
      ConsistentRead: true,
    })) as GetCommandOutput;
    if (result.Item) {
      const item = modelConfig(result.Item);
      this.modelItemCache.set(name, { expiresAt: now + 60, item });
      return item;
    }
    return undefined;
  }

  async getPoolConfig(model: string, tier: string): Promise<PoolConfigRecord> {
    const key = poolKey(model, tier);
    const cached = this.poolConfigCache.get(key);
    const now = this.clock();
    if (cached && cached.expiresAt > now) return cached.item;

    const result = await this.ddb.send(new GetCommand({
      TableName: this.tableName,
      Key: { pool_key: key, record_key: "CONFIG" },
      ConsistentRead: true,
    })) as GetCommandOutput;
    if (!result.Item) throw new Error(`Missing pool configuration for ${model}/${tier}`);
    const item = poolConfigRecord(result.Item);
    this.poolConfigCache.set(key, { expiresAt: now + 60, item });
    return item;
  }

  async queryInstances(model: string, tier: string): Promise<InstanceRecord[]> {
    return (await this.queryPoolRecords(model, tier)).filter(
      isInstanceRecord,
    );
  }

  async queryPoolRecords(model: string, tier: string): Promise<DatabaseRecord[]> {
    const items: DatabaseRecord[] = [];
    let ExclusiveStartKey: Record<string, unknown> | undefined;
    do {
      const result = await this.ddb.send(new QueryCommand({
        TableName: this.tableName,
        KeyConditionExpression: "pool_key = :pool",
        ExpressionAttributeValues: { ":pool": poolKey(model, tier) },
        ConsistentRead: true,
        ExclusiveStartKey,
      })) as QueryCommandOutput;
      items.push(...(result.Items ?? []).map(databaseRecord));
      ExclusiveStartKey = result.LastEvaluatedKey;
    } while (ExclusiveStartKey);
    return items;
  }

  private async sendTransaction(
    input: TransactWriteCommandInput,
    attempts = 3,
  ): Promise<void> {
    let lastError: unknown;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        await this.ddb.send(new TransactWriteCommand(input));
        return;
      } catch (error) {
        lastError = error;
        if (!retryableTransactionFailure(error) || attempt === attempts - 1) throw error;
        await this.sleep((50 * (2 ** attempt)) + Math.floor(Math.random() * 50));
      }
    }
    throw lastError ?? new Error("DynamoDB transaction exhausted its retry budget");
  }

  async claimInstance(
    instance: InstanceRecord,
    requestId: string,
    minimumHardExpiry: number,
  ): Promise<InstanceRecord | undefined> {
    const now = this.clock();
    const leaseExpiresAt = now + this.leaseSeconds;
    const softExpiresAt = Math.min(now + this.softTtlSeconds, instance.hard_expires_at);
    const instanceKey = {
      pool_key: instance.pool_key,
      record_key: instance.record_key,
    };
    const leaseKey = {
      pool_key: instance.pool_key,
      record_key: `LEASE#${requestId}`,
    };
    try {
      await this.sendTransaction({
        ClientRequestToken: transactionToken(`claim:${requestId}:${instance.record_key}`),
        TransactItems: [
          {
            Update: {
              TableName: this.tableName,
              Key: instanceKey,
              ConditionExpression: [
                "#state = :ready",
                "(#status = :available OR #status = :busy)",
                "in_flight < #capacity",
                "soft_expires_at > :now",
                "hard_expires_at > :minimum_hard",
              ].join(" AND "),
              UpdateExpression: [
                "SET #status = :busy, last_used_at = :now,",
                "soft_expires_at = :soft_expires, ttl_at = :soft_expires",
                "ADD in_flight :one",
              ].join(" "),
              ExpressionAttributeNames: {
                "#state": "state",
                "#status": "status",
                "#capacity": "capacity",
              },
              ExpressionAttributeValues: {
                ":ready": READY,
                ":available": AVAILABLE,
                ":busy": BUSY,
                ":now": now,
                ":minimum_hard": minimumHardExpiry,
                ":soft_expires": softExpiresAt,
                ":one": 1,
              },
            },
          },
          {
            Put: {
              TableName: this.tableName,
              Item: {
                ...leaseKey,
                entity: "LEASE",
                request_id: requestId,
                microvm_id: instance.microvm_id,
                lease_expires_at: leaseExpiresAt,
                ttl_at: leaseExpiresAt + 300,
              },
              ConditionExpression: "attribute_not_exists(pool_key)",
            },
          },
        ],
      });
      return {
        ...instance,
        status: BUSY,
        in_flight: (instance.in_flight ?? 0) + 1,
        soft_expires_at: softExpiresAt,
        ttl_at: softExpiresAt,
      };
    } catch (error) {
      if (conditionalFailure(error)) return undefined;
      throw error;
    }
  }

  async acquireCompatible(
    model: ModelConfig,
    tiers: readonly TierConfig[],
    firstTierIndex: number,
    requestId: string,
    predictedRuntimeSeconds: number,
  ): Promise<InstanceRecord | undefined> {
    const minimumHardExpiry = this.clock() + Math.max(
      predictedRuntimeSeconds + 60,
      this.hardExpiryHeadroomSeconds,
    );
    for (const tier of tiers.slice(firstTierIndex)) {
      const instances = await this.queryInstances(model.name, tier.name);
      const candidates = instances
        .filter((instance) => (
          instance.state === READY
          && (instance.status === AVAILABLE || instance.status === BUSY)
          && instance.in_flight < instance.capacity
          && instance.soft_expires_at > this.clock()
          && instance.hard_expires_at > minimumHardExpiry
        ))
        .sort((left, right) => (right.last_used_at ?? 0) - (left.last_used_at ?? 0));
      for (const candidate of candidates) {
        const claimed = await this.claimInstance(candidate, requestId, minimumHardExpiry);
        if (claimed) return claimed;
      }
    }
    return undefined;
  }

  async acquireLaunchLock(config: PoolConfigRecord, requestId: string): Promise<boolean> {
    const now = this.clock();
    try {
      await this.ddb.send(new UpdateCommand({
        TableName: this.tableName,
        Key: { pool_key: config.pool_key, record_key: "LOCK#LAUNCH" },
        ConditionExpression: "attribute_not_exists(lock_until) OR lock_until < :now",
        UpdateExpression: "SET #entity = :entity, lock_owner = :owner, lock_until = :until, ttl_at = :ttl",
        ExpressionAttributeNames: { "#entity": "entity" },
        ExpressionAttributeValues: {
          ":entity": "LOCK",
          ":owner": requestId,
          ":until": now + this.launchLockSeconds,
          ":ttl": now + this.launchLockSeconds + 300,
          ":now": now,
        },
      }));
      return true;
    } catch (error) {
      if (conditionalFailure(error)) return false;
      throw error;
    }
  }

  async releaseLaunchLock(config: PoolConfigRecord, requestId: string): Promise<void> {
    try {
      await this.ddb.send(new DeleteCommand({
        TableName: this.tableName,
        Key: { pool_key: config.pool_key, record_key: "LOCK#LAUNCH" },
        ConditionExpression: "lock_owner = :owner",
        ExpressionAttributeValues: { ":owner": requestId },
      }));
    } catch (error) {
      if (!conditionalFailure(error)) throw error;
    }
  }

  async launchAndClaim(
    model: ModelConfig,
    tier: TierConfig,
    requestId: string,
    predictedRuntimeSeconds = 0,
  ): Promise<InstanceRecord | undefined> {
    const config = await this.getPoolConfig(model.name, tier.name);
    if (!(await this.acquireLaunchLock(config, requestId))) return undefined;

    const reservationId = randomUUID();
    const reservationKey = {
      pool_key: config.pool_key,
      record_key: `RESERVATION#${reservationId}`,
    };
    let launched: Awaited<ReturnType<MicrovmService["launch"]>> | undefined;
    try {
      const now = this.clock();
      const records = await this.queryPoolRecords(model.name, tier.name);
      // TTL removal is asynchronous. Logically expired idle rows must not hold
      // a pool slot for minutes after their native idle policy has made them
      // unroutable (and normally already terminated them). An in-flight row
      // still counts defensively even if a future configuration makes the soft
      // window shorter than the maximum request duration.
      const active = records.filter(isInstanceRecord).filter((instance) => (
        instance.state !== "FAILED"
        && instance.hard_expires_at > now
        && (instance.soft_expires_at > now || instance.in_flight > 0)
      ));
      const minimumHardExpiry = now + Math.max(
        predictedRuntimeSeconds + 60,
        this.hardExpiryHeadroomSeconds,
      );
      const newlyAvailable = active
        .filter((instance) => (
          instance.record_key.startsWith("INSTANCE#")
          && instance.state === READY
          && (instance.status === AVAILABLE || instance.status === BUSY)
          && instance.in_flight < instance.capacity
          && instance.soft_expires_at > now
          && instance.hard_expires_at > minimumHardExpiry
        ))
        .sort((left, right) => (right.last_used_at ?? 0) - (left.last_used_at ?? 0));
      for (const candidate of newlyAvailable) {
        const claimed = await this.claimInstance(candidate, requestId, minimumHardExpiry);
        if (claimed) return claimed;
      }

      const pendingReservations = records.filter((item) => (
        item.entity === "RESERVATION"
        && typeof item.ttl_at === "number"
        && item.ttl_at > now
      ));
      if (active.length + pendingReservations.length >= config.max_instances) return undefined;

      const clientToken = transactionToken(`launch:${config.pool_key}:${reservationId}`);
      await this.ddb.send(new PutCommand({
        TableName: this.tableName,
        Item: {
          ...reservationKey,
          entity: "RESERVATION",
          status: "LAUNCHING",
          request_id: requestId,
          launch_token: clientToken,
          image_arn: config.image_arn,
          image_version: config.image_version,
          created_at: now,
          ttl_at: now + 300,
        },
        ConditionExpression: "attribute_not_exists(pool_key)",
      }));

      launched = await this.microvms.launch(config, clientToken);
      await this.ddb.send(new UpdateCommand({
        TableName: this.tableName,
        Key: reservationKey,
        UpdateExpression: "SET microvm_id = :id, endpoint = :endpoint",
        ExpressionAttributeValues: {
          ":id": launched.microvmId,
          ":endpoint": launched.endpoint,
        },
      }));

      launched = await this.microvms.waitUntilRunning(launched);
      const auth = await this.microvms.createAuthToken(launched.microvmId, this.tokenMinutes);
      launched = await this.microvms.waitUntilReady(launched, auth.token);
      const readyAt = this.clock();
      const platformStartedAt = launched.startedAt
        ? Math.floor(new Date(launched.startedAt).getTime() / 1000)
        : now;
      const hardExpiresAt = platformStartedAt + this.hardTtlSeconds;
      const softExpiresAt = Math.min(readyAt + this.softTtlSeconds, hardExpiresAt);
      const leaseExpiresAt = readyAt + this.leaseSeconds;
      const instance: InstanceRecord = {
        pool_key: config.pool_key,
        record_key: `INSTANCE#${launched.microvmId}`,
        entity: "INSTANCE",
        model: model.name,
        tier: tier.name,
        tier_order: tier.order,
        state: READY,
        status: BUSY,
        in_flight: 1,
        capacity: config.capacity ?? 1,
        microvm_id: launched.microvmId,
        endpoint: launched.endpoint,
        image_arn: launched.imageArn ?? config.image_arn,
        image_version: launched.imageVersion ?? config.image_version,
        auth_token: auth.token,
        auth_expires_at: auth.expiresAt,
        created_at: platformStartedAt,
        ready_at: readyAt,
        last_used_at: readyAt,
        soft_expires_at: softExpiresAt,
        hard_expires_at: hardExpiresAt,
        ttl_at: softExpiresAt,
      };
      await this.sendTransaction({
        ClientRequestToken: transactionToken(`register:${requestId}:${launched.microvmId}`),
        TransactItems: [
          {
            Put: {
              TableName: this.tableName,
              Item: instance,
              ConditionExpression: "attribute_not_exists(pool_key)",
            },
          },
          {
            Put: {
              TableName: this.tableName,
              Item: {
                pool_key: config.pool_key,
                record_key: `LEASE#${requestId}`,
                entity: "LEASE",
                request_id: requestId,
                microvm_id: launched.microvmId,
                lease_expires_at: leaseExpiresAt,
                ttl_at: leaseExpiresAt + 300,
              },
              ConditionExpression: "attribute_not_exists(pool_key)",
            },
          },
          {
            Delete: {
              TableName: this.tableName,
              Key: reservationKey,
            },
          },
        ],
      });
      return instance;
    } catch (error) {
      if (launched?.microvmId) {
        await this.microvms.terminate(launched.microvmId).catch(() => undefined);
      }
      await this.ddb.send(new DeleteCommand({
        TableName: this.tableName,
        Key: reservationKey,
      })).catch(() => undefined);
      throw error;
    } finally {
      await this.releaseLaunchLock(config, requestId);
    }
  }

  async waitForCapacity(
    model: ModelConfig,
    tiers: readonly TierConfig[],
    firstTierIndex: number,
    requestId: string,
    predictedRuntimeSeconds: number,
  ): Promise<InstanceRecord | undefined> {
    // A cold run takes about four seconds in the measured images. Wait long
    // enough for the reservation owner to publish it, and periodically retry
    // the launch lock so bursts can grow to max_instances without racing.
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const instance = await this.acquireCompatible(
        model,
        tiers,
        firstTierIndex,
        requestId,
        predictedRuntimeSeconds,
      );
      if (instance) return instance;
      if (attempt > 0 && attempt % 2 === 0) {
        const firstTier = tiers[firstTierIndex];
        if (!firstTier) return undefined;
        const launched = await this.launchAndClaim(
          model,
          firstTier,
          requestId,
          predictedRuntimeSeconds,
        );
        if (launched) return launched;
      }
      if (attempt < 19) await this.sleep(450 + Math.floor(Math.random() * 100));
    }
    return undefined;
  }

  async ensureAuthToken(
    instance: InstanceRecord,
    _requestId: string,
    { force = false }: { readonly force?: boolean } = {},
  ): Promise<string> {
    const now = this.clock();
    const refreshBeforeExpirySeconds = Math.max(
      60,
      (this.tokenMinutes - this.tokenRefreshMinutes) * 60,
    );
    if (!force && instance.auth_token
      && instance.auth_expires_at !== undefined
      && instance.auth_expires_at > now + refreshBeforeExpirySeconds) {
      return instance.auth_token;
    }
    const auth = await this.microvms.createAuthToken(instance.microvm_id, this.tokenMinutes);
    await this.ddb.send(new UpdateCommand({
      TableName: this.tableName,
      Key: { pool_key: instance.pool_key, record_key: instance.record_key },
      ConditionExpression: "#state = :ready AND microvm_id = :microvm",
      UpdateExpression: "SET auth_token = :token, auth_expires_at = :expires",
      ExpressionAttributeNames: { "#state": "state" },
      ExpressionAttributeValues: {
        ":ready": READY,
        ":microvm": instance.microvm_id,
        ":token": auth.token,
        ":expires": auth.expiresAt,
      },
    })).catch((error: unknown) => {
      // The freshly issued token is usable even if its distributed cache write
      // fails. Do not kill a healthy worker for optional token bookkeeping.
      console.warn(JSON.stringify({
        event: "auth_token_cache_write_failed",
        microvm_id: instance.microvm_id,
        error: errorMessage(error),
      }));
    });
    instance.auth_token = auth.token;
    instance.auth_expires_at = auth.expiresAt;
    return auth.token;
  }

  async release(instance: InstanceRecord, requestId: string): Promise<void> {
    const now = this.clock();
    try {
      await this.sendTransaction({
        ClientRequestToken: transactionToken(`release:${requestId}:${instance.microvm_id}`),
        TransactItems: [
          {
            Delete: {
              TableName: this.tableName,
              Key: {
                pool_key: instance.pool_key,
                record_key: `LEASE#${requestId}`,
              },
              ConditionExpression: "request_id = :owner",
              ExpressionAttributeValues: { ":owner": requestId },
            },
          },
          {
            Update: {
              TableName: this.tableName,
              Key: { pool_key: instance.pool_key, record_key: instance.record_key },
              ConditionExpression: "#state = :ready AND in_flight > :zero",
              UpdateExpression: [
                "SET last_completed_at = :now",
                "ADD in_flight :minus_one",
              ].join(" "),
              ExpressionAttributeNames: { "#state": "state" },
              ExpressionAttributeValues: {
                ":ready": READY,
                ":zero": 0,
                ":minus_one": -1,
                ":now": now,
              },
            },
          },
        ],
      });
      await this.ddb.send(new UpdateCommand({
        TableName: this.tableName,
        Key: { pool_key: instance.pool_key, record_key: instance.record_key },
        ConditionExpression: "#state = :ready AND in_flight = :zero",
        UpdateExpression: "SET #status = :available",
        ExpressionAttributeNames: { "#state": "state", "#status": "status" },
        ExpressionAttributeValues: { ":ready": READY, ":zero": 0, ":available": AVAILABLE },
      })).catch((error: unknown) => {
        if (!conditionalFailure(error)) throw error;
      });
    } catch (error) {
      if (!conditionalFailure(error)) throw error;
      // A peer may already have fenced this worker. Its lease can still be
      // removed without mutating the failed instance's occupancy/state.
      await this.ddb.send(new DeleteCommand({
        TableName: this.tableName,
        Key: { pool_key: instance.pool_key, record_key: `LEASE#${requestId}` },
      })).catch(() => undefined);
    }
  }

  async fail(instance: InstanceRecord, requestId: string, reason: string): Promise<void> {
    const now = this.clock();
    const values = {
      ":failed": "FAILED",
      ":unavailable": "UNAVAILABLE",
      ":reason": String(reason).slice(0, 500),
      ":ttl": now + 300,
      ":zero": 0,
      ":minus_one": -1,
    };
    try {
      await this.sendTransaction({
        ClientRequestToken: transactionToken(`fail:${requestId}:${instance.microvm_id}`),
        TransactItems: [
          {
            Update: {
              TableName: this.tableName,
              Key: { pool_key: instance.pool_key, record_key: instance.record_key },
              ConditionExpression: "in_flight > :zero",
              UpdateExpression: [
                "SET #state = :failed, #status = :unavailable, failure_reason = :reason,",
                "ttl_at = :ttl ADD in_flight :minus_one",
              ].join(" "),
              ExpressionAttributeNames: { "#state": "state", "#status": "status" },
              ExpressionAttributeValues: values,
            },
          },
          {
            Delete: {
              TableName: this.tableName,
              Key: { pool_key: instance.pool_key, record_key: `LEASE#${requestId}` },
              ConditionExpression: "request_id = :owner",
              ExpressionAttributeValues: { ":owner": requestId },
            },
          },
        ],
      });
    } catch (error) {
      // Even if the caller's lease was already recovered, fence the worker so
      // no new requests arrive while endpoint termination is in flight.
      await this.ddb.send(new UpdateCommand({
        TableName: this.tableName,
        Key: { pool_key: instance.pool_key, record_key: instance.record_key },
        UpdateExpression: [
          "SET #state = :failed, #status = :unavailable, failure_reason = :reason,",
          "ttl_at = :ttl",
        ].join(" "),
        ExpressionAttributeNames: { "#state": "state", "#status": "status" },
        ExpressionAttributeValues: {
          ":failed": values[":failed"],
          ":unavailable": values[":unavailable"],
          ":reason": values[":reason"],
          ":ttl": values[":ttl"],
        },
      })).catch(() => undefined);
      await this.ddb.send(new DeleteCommand({
        TableName: this.tableName,
        Key: { pool_key: instance.pool_key, record_key: `LEASE#${requestId}` },
      })).catch(() => undefined);
    }
    await this.microvms.terminate(instance.microvm_id).catch(() => undefined);
  }
}
