import type {
  DatabaseRecord,
  InstanceRecord,
  ModelConfig,
  PoolConfigRecord,
  TierConfig,
} from "./types.js";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireString(record: Record<string, unknown>, key: string, entity: string): string {
  const value = record[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`Invalid ${entity} record: ${key} must be a non-empty string`);
  }
  return value;
}

function requireNumber(record: Record<string, unknown>, key: string, entity: string): number {
  const value = record[key];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`Invalid ${entity} record: ${key} must be a finite number`);
  }
  return value;
}

function optionalNumber(record: Record<string, unknown>, key: string): number | undefined {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function tierConfig(value: unknown): TierConfig {
  if (!isRecord(value)) throw new Error("Invalid model record: tier must be an object");
  return {
    name: requireString(value, "name", "tier"),
    order: requireNumber(value, "order", "tier"),
    max_item_tokens: requireNumber(value, "max_item_tokens", "tier"),
    max_total_tokens: requireNumber(value, "max_total_tokens", "tier"),
    max_batch_items: requireNumber(value, "max_batch_items", "tier"),
    max_attention_score: requireNumber(value, "max_attention_score", "tier"),
    max_response_bytes: requireNumber(value, "max_response_bytes", "tier"),
  };
}

export function modelConfig(value: unknown): ModelConfig {
  if (!isRecord(value)) throw new Error("Invalid model record: expected an object");
  const tiers = value.tiers;
  if (!Array.isArray(tiers)) throw new Error("Invalid model record: tiers must be an array");
  const maxTokens = optionalNumber(value, "max_tokens");
  const order = optionalNumber(value, "order");
  return {
    name: requireString(value, "name", "model"),
    type: requireString(value, "type", "model"),
    dimensions: requireNumber(value, "dimensions", "model"),
    ...(maxTokens === undefined ? {} : { max_tokens: maxTokens }),
    ...(order === undefined ? {} : { order }),
    tiers: tiers.map(tierConfig),
  };
}

export function databaseRecord(value: unknown): DatabaseRecord {
  if (!isRecord(value)) throw new Error("Invalid DynamoDB item: expected an object");
  return {
    ...value,
    pool_key: requireString(value, "pool_key", "DynamoDB"),
    record_key: requireString(value, "record_key", "DynamoDB"),
  };
}

export function poolConfigRecord(value: unknown): PoolConfigRecord {
  const record = databaseRecord(value);
  if (record.entity !== "POOL") throw new Error("Invalid pool record: entity must be POOL");
  const capacity = optionalNumber(record, "capacity");
  const idleSeconds = optionalNumber(record, "idle_seconds");
  const suspendedSeconds = optionalNumber(record, "suspended_seconds");
  return {
    ...record,
    entity: "POOL",
    image_arn: requireString(record, "image_arn", "pool"),
    image_version: requireString(record, "image_version", "pool"),
    max_instances: requireNumber(record, "max_instances", "pool"),
    ...(capacity === undefined ? {} : { capacity }),
    ...(idleSeconds === undefined ? {} : { idle_seconds: idleSeconds }),
    ...(suspendedSeconds === undefined ? {} : { suspended_seconds: suspendedSeconds }),
  };
}

export function isInstanceRecord(value: unknown): value is InstanceRecord {
  if (!isRecord(value)) return false;
  return typeof value.pool_key === "string"
    && typeof value.record_key === "string"
    && value.record_key.startsWith("INSTANCE#")
    && typeof value.model === "string"
    && typeof value.tier === "string"
    && typeof value.state === "string"
    && typeof value.status === "string"
    && typeof value.in_flight === "number"
    && typeof value.capacity === "number"
    && typeof value.microvm_id === "string"
    && typeof value.endpoint === "string"
    && typeof value.soft_expires_at === "number"
    && typeof value.hard_expires_at === "number";
}

export function instanceRecord(value: unknown): InstanceRecord {
  if (!isInstanceRecord(value)) throw new Error("Invalid DynamoDB instance record");
  return value;
}
