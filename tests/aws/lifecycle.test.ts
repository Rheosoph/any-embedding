import assert from "node:assert/strict";
import test from "node:test";

import {
  createLifecycleService,
  isTtlServiceRemoval,
} from "../../app/aws/lifecycle/service.js";

const lifecycle = createLifecycleService({
  ddb: { send: async () => ({}) },
  microvms: {
    get: async () => {
      throw new Error("unexpected MicroVM get");
    },
    list: async () => ({ items: [] }),
    terminate: async () => undefined,
  },
  tableName: "state",
  executionRoleArn: "arn:example:role",
  orphanGraceSeconds: 600,
});

test("only DynamoDB TTL service removals drive lifecycle recovery", async () => {
  const ttlRemoval = {
    eventName: "REMOVE",
    userIdentity: {
      type: "Service",
      principalId: "dynamodb.amazonaws.com",
    },
  };
  assert.equal(isTtlServiceRemoval(ttlRemoval), true);
  assert.equal(isTtlServiceRemoval({ eventName: "REMOVE" }), false);
  assert.equal(isTtlServiceRemoval({ ...ttlRemoval, eventName: "MODIFY" }), false);

  // An ordinary gateway transaction also emits REMOVE for a lease. It must be
  // ignored or a capacity>1 worker would be decremented twice.
  const result = await lifecycle.processStream([{
    eventName: "REMOVE",
    userIdentity: { type: "IAMUser", principalId: "gateway" },
    dynamodb: { OldImage: { entity: { S: "LEASE" } } },
  }]);
  assert.deepEqual(result, { batchItemFailures: [] });
});
