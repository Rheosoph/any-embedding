import assert from "node:assert/strict";
import test from "node:test";

import {
  GetCommand,
  TransactWriteCommand,
  UpdateCommand,
  type DynamoClient,
  type DynamoCommand,
  type MicrovmService,
} from "../../app/aws/dependencies.js";
import { PoolRegistry } from "../../app/aws/gateway/pool.js";
import type {
  InstanceRecord,
  ModelConfig,
  PoolConfigRecord,
  TierConfig,
} from "../../app/aws/types.js";

const model = {
  name: "gte-multilingual-base",
  type: "text",
  dimensions: 768,
  tiers: [],
} satisfies ModelConfig;

function tier(name: string, order: number): TierConfig {
  return {
    name,
    order,
    max_item_tokens: 8_192,
    max_total_tokens: 250_000,
    max_batch_items: 2_048,
    max_attention_score: 2_048 * 8_192 * 8_192,
    max_response_bytes: 200_000_000,
  };
}

const unusedDdb: DynamoClient = {
  send: async () => {
    throw new Error("Unexpected DynamoDB command");
  },
};

function microvmStub(overrides: Partial<MicrovmService> = {}): MicrovmService {
  return {
    launch: async () => {
      throw new Error("Unexpected MicroVM launch");
    },
    createAuthToken: async () => {
      throw new Error("Unexpected auth-token creation");
    },
    waitUntilRunning: async () => {
      throw new Error("Unexpected running wait");
    },
    waitUntilReady: async () => {
      throw new Error("Unexpected readiness wait");
    },
    invoke: async () => {
      throw new Error("Unexpected MicroVM invocation");
    },
    get: async () => {
      throw new Error("Unexpected MicroVM get");
    },
    list: async () => {
      throw new Error("Unexpected MicroVM list");
    },
    terminate: async () => {
      throw new Error("Unexpected MicroVM termination");
    },
    ...overrides,
  };
}

function transactionCommand(command: DynamoCommand | undefined): TransactWriteCommand {
  assert.ok(command instanceof TransactWriteCommand);
  return command;
}

function updateCommand(command: DynamoCommand | undefined): UpdateCommand {
  assert.ok(command instanceof UpdateCommand);
  return command;
}

function getCommand(command: DynamoCommand | undefined): GetCommand {
  assert.ok(command instanceof GetCommand);
  return command;
}

function readyInstance(overrides: Partial<InstanceRecord> = {}): InstanceRecord {
  return {
    pool_key: "POOL#gte-multilingual-base#tier-1",
    record_key: "INSTANCE#microvm-1",
    entity: "INSTANCE",
    model: "gte-multilingual-base",
    tier: "tier-1",
    state: "READY",
    status: "AVAILABLE",
    in_flight: 0,
    capacity: 1,
    microvm_id: "microvm-1",
    endpoint: "example.invalid",
    hard_expires_at: 26000,
    soft_expires_at: 2000,
    last_used_at: 900,
    ...overrides,
  };
}

test("claim is an atomic instance update plus owned lease", async () => {
  const commands: DynamoCommand[] = [];
  const registry = new PoolRegistry({
    ddb: { send: async (command: DynamoCommand) => { commands.push(command); return {}; } },
    microvms: microvmStub(),
    tableName: "state",
    clock: () => 1000,
  });
  const claimed = await registry.claimInstance(
    readyInstance(),
    "request-1",
    1100,
  );
  assert.ok(claimed);
  assert.equal(claimed.status, "BUSY");
  assert.equal(claimed.in_flight, 1);
  assert.equal(commands.length, 1);
  const transaction = transactionCommand(commands[0]).input.TransactItems;
  assert.ok(transaction);
  assert.equal(transaction.length, 2);
  assert.ok(transaction[0]?.Update);
  assert.match(transaction[0].Update.ConditionExpression ?? "", /in_flight < #capacity/);
  assert.ok(transaction[1]?.Put);
  const lease = transaction[1].Put.Item;
  assert.ok(lease);
  assert.equal(lease.record_key, "LEASE#request-1");
  assert.equal(lease.lease_expires_at, 1900);
});

test("pool safety defaults cover the full Lambda execution window", () => {
  const registry = new PoolRegistry({
    ddb: unusedDdb,
    microvms: microvmStub(),
    tableName: "state",
  });
  assert.equal(registry.leaseSeconds, 900);
  assert.equal(registry.tokenMinutes, 60);
  assert.equal(registry.tokenRefreshMinutes, 55);
  assert.equal(registry.launchLockSeconds, 180);
  assert.equal(registry.hardExpiryHeadroomSeconds, 900);
});

test("embedding model lookup uses keyed reads and caches without scanning state", async () => {
  const commands: DynamoCommand[] = [];
  const storedModel = {
    ...model,
    entity: "MODEL",
  };
  const registry = new PoolRegistry({
    ddb: {
      send: async (command: DynamoCommand) => {
        commands.push(command);
        return { Item: storedModel };
      },
    },
    microvms: microvmStub(),
    tableName: "state",
    clock: () => 1000,
  });

  assert.deepEqual(await registry.getModel(model.name), model);
  assert.deepEqual(await registry.getModel(model.name), model);
  assert.equal(commands.length, 1);
  assert.deepEqual(getCommand(commands[0]).input.Key, {
    pool_key: "MODEL#gte-multilingual-base",
    record_key: "CONFIG",
  });
});

test("release deletes only its lease and decrements occupancy", async () => {
  const commands: DynamoCommand[] = [];
  const registry = new PoolRegistry({
    ddb: { send: async (command: DynamoCommand) => { commands.push(command); return {}; } },
    microvms: microvmStub(),
    tableName: "state",
    clock: () => 1200,
  });
  await registry.release(readyInstance({ in_flight: 1 }), "request-1");
  const transaction = transactionCommand(commands[0]).input.TransactItems;
  assert.ok(transaction);
  assert.equal(
    transaction[0]?.Delete?.Key?.record_key,
    "LEASE#request-1",
  );
  assert.equal(
    transaction[1]?.Update?.ExpressionAttributeValues?.[":minus_one"],
    -1,
  );
  const update = updateCommand(commands[1]);
  assert.match(update.input.ConditionExpression ?? "", /in_flight = :zero/);
  assert.doesNotMatch(
    transaction[1]?.Update?.UpdateExpression ?? "",
    /soft_expires_at|ttl_at/,
  );
});

test("compatible acquisition packs work onto the most recent larger running tier", async () => {
  const registry = new PoolRegistry({
    ddb: unusedDdb,
    microvms: microvmStub(),
    tableName: "state",
    clock: () => 1000,
  });
  const old = readyInstance({ record_key: "INSTANCE#old", last_used_at: 800 });
  const recent = readyInstance({ record_key: "INSTANCE#recent", last_used_at: 950 });
  const queried: string[] = [];
  registry.queryInstances = async (_model: string, tierName: string) => {
    queried.push(tierName);
    return tierName === "tier-2" ? [old, recent] : [];
  };
  registry.claimInstance = async (candidate: InstanceRecord) => ({
    ...candidate,
    status: "BUSY",
  });
  const result = await registry.acquireCompatible(
    model,
    [tier("tier-1", 1), tier("tier-2", 2), tier("tier-3", 3)],
    0,
    "request-1",
    10,
  );
  assert.deepEqual(queried, ["tier-1", "tier-2"]);
  assert.equal(result?.record_key, "INSTANCE#recent");
});

test("logically expired instances are never claimed even if DynamoDB has not deleted them", async () => {
  const registry = new PoolRegistry({
    ddb: unusedDdb,
    microvms: microvmStub(),
    tableName: "state",
    clock: () => 1000,
  });
  registry.queryInstances = async () => [readyInstance({ soft_expires_at: 999 })];
  registry.claimInstance = async () => assert.fail("expired instance was claimed");
  const result = await registry.acquireCompatible(
    model,
    [tier("tier-1", 1)],
    0,
    "request-1",
    10,
  );
  assert.equal(result, undefined);
});

test("expired rows awaiting asynchronous TTL deletion do not consume launch capacity", async () => {
  const commands: DynamoCommand[] = [];
  let launches = 0;
  const registry = new PoolRegistry({
    ddb: { send: async (command: DynamoCommand) => { commands.push(command); return {}; } },
    microvms: microvmStub({
      launch: async () => {
        launches += 1;
        return {
          microvmId: "microvm-new",
          endpoint: "new.example.invalid",
          state: "PENDING",
          startedAt: new Date(900_000),
        };
      },
      waitUntilRunning: async (launched) => ({ ...launched, state: "RUNNING" }),
      createAuthToken: async () => ({ token: "token", expiresAt: 4600 }),
      waitUntilReady: async (launched) => launched,
      terminate: async () => undefined,
    }),
    tableName: "state",
    clock: () => 1000,
  });
  registry.getPoolConfig = async () => ({
    pool_key: "POOL#gte-multilingual-base#tier-1",
    record_key: "CONFIG",
    entity: "POOL",
    image_arn: "arn:example:image",
    image_version: "2.0",
    max_instances: 2,
    capacity: 2,
  } satisfies PoolConfigRecord);
  registry.acquireLaunchLock = async () => true;
  registry.releaseLaunchLock = async () => undefined;
  registry.queryPoolRecords = async () => [
    readyInstance({ record_key: "INSTANCE#expired-1", soft_expires_at: 999 }),
    readyInstance({ record_key: "INSTANCE#expired-2", soft_expires_at: 998 }),
  ];

  const result = await registry.launchAndClaim(
    model,
    tier("tier-1", 1),
    "request-1",
  );
  assert.equal(launches, 1);
  assert.equal(result?.microvm_id, "microvm-new");
  assert.ok(commands.some((command) => command instanceof TransactWriteCommand));
});

test("capacity wait periodically retries the launch lock during a cold burst", async () => {
  let acquisitions = 0;
  let launches = 0;
  let sleeps = 0;
  const registry = new PoolRegistry({
    ddb: unusedDdb,
    microvms: microvmStub(),
    tableName: "state",
    sleep: async () => { sleeps += 1; },
  });
  registry.acquireCompatible = async () => {
    acquisitions += 1;
    return undefined;
  };
  registry.launchAndClaim = async () => {
    launches += 1;
    return launches === 2 ? readyInstance() : undefined;
  };
  const result = await registry.waitForCapacity(
    model,
    [tier("tier-1", 1)],
    0,
    "request-1",
    10,
  );
  assert.equal(result?.microvm_id, "microvm-1");
  assert.equal(launches, 2);
  assert.equal(acquisitions, 5);
  assert.equal(sleeps, 4);
});

test("auth cache refresh cannot recreate a deleted or fenced instance row", async () => {
  const commands: DynamoCommand[] = [];
  const registry = new PoolRegistry({
    ddb: { send: async (command: DynamoCommand) => { commands.push(command); return {}; } },
    microvms: microvmStub({
      createAuthToken: async () => ({ token: "fresh-token", expiresAt: 4600 }),
    }),
    tableName: "state",
    clock: () => 1000,
  });
  const instance = readyInstance();
  assert.equal(await registry.ensureAuthToken(instance, "request-1"), "fresh-token");
  const update = updateCommand(commands[0]);
  assert.match(update.input.ConditionExpression ?? "", /microvm_id = :microvm/);
  assert.equal(update.input.ExpressionAttributeValues?.[":microvm"], "microvm-1");
});
