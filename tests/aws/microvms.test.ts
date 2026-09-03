import assert from "node:assert/strict";
import test from "node:test";

import {
  RunMicrovmCommand,
  type MicrovmCommand,
} from "../../app/aws/dependencies.js";
import { MicrovmController } from "../../app/aws/gateway/microvms.js";

type RunMicrovmCommandInput = RunMicrovmCommand["input"];

test("launch makes the service-default internet egress explicit", async () => {
  let input: RunMicrovmCommandInput | undefined;
  const controller = new MicrovmController({
    region: "eu-central-1",
    client: {
      send: async (command: MicrovmCommand) => {
        if (!(command instanceof RunMicrovmCommand)) {
          throw new Error("Expected RunMicrovmCommand");
        }
        input = command.input;
        return {
          microvmId: "microvm-test",
          endpoint: "example.invalid",
          state: "PENDING",
        };
      },
    },
  });
  await controller.launch({
    image_arn: "arn:aws:lambda:eu-central-1:123456789012:microvm-image:test",
    image_version: "2.0",
    pool_key: "POOL#test#small",
  }, "client-token");
  assert.ok(input);
  assert.deepEqual(input.egressNetworkConnectors, [
    "arn:aws:lambda:eu-central-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS",
  ]);
});

test("new MicroVMs reach RUNNING before token creation", async () => {
  let now = 0;
  let calls = 0;
  const controller = new MicrovmController({
    client: {
      send: async () => {
        calls += 1;
        return {
          microvmId: "microvm-test",
          endpoint: "example.invalid",
          state: calls === 1 ? "PENDING" : "RUNNING",
        };
      },
    },
    nowMilliseconds: () => now,
    sleep: async (milliseconds: number) => { now += milliseconds; },
    readyTimeoutSeconds: 10,
  });
  const result = await controller.waitUntilRunning({
    microvmId: "microvm-test",
    endpoint: "example.invalid",
    state: "PENDING",
  });
  assert.equal(result.state, "RUNNING");
  assert.equal(calls, 2);
});

test("new MicroVMs are endpoint-probed before they become routable", async () => {
  let now = 0;
  let controlCalls = 0;
  let endpointCalls = 0;
  const client = {
    send: async () => {
      controlCalls += 1;
      return {
        microvmId: "microvm-test",
        endpoint: "example.invalid",
        state: controlCalls === 1 ? "PENDING" : "RUNNING",
        startedAt: new Date(1_000),
      };
    },
  };
  const controller = new MicrovmController({
    client,
    nowMilliseconds: () => now,
    sleep: async (milliseconds: number) => { now += milliseconds; },
    readyTimeoutSeconds: 10,
    fetchImpl: async () => {
      endpointCalls += 1;
      return new Response(null, { status: endpointCalls === 1 ? 502 : 200 });
    },
  });

  const result = await controller.waitUntilReady({
    microvmId: "microvm-test",
    endpoint: "example.invalid",
    state: "PENDING",
  }, "token");
  assert.equal(result.state, "RUNNING");
  assert.equal(endpointCalls, 2);
  assert.equal(controlCalls, 2);
});

test("control-plane reads retry throttling during cold bursts", async () => {
  let calls = 0;
  let slept = 0;
  const controller = new MicrovmController({
    client: {
      send: async () => {
        calls += 1;
        if (calls === 1) {
          const error = new Error("slow down");
          error.name = "ThrottlingException";
          throw error;
        }
        return {
          microvmId: "microvm-test",
          endpoint: "example.invalid",
          state: "RUNNING",
        };
      },
    },
    sleep: async (milliseconds: number) => { slept += milliseconds; },
  });
  assert.equal((await controller.get("microvm-test")).state, "RUNNING");
  assert.equal(calls, 2);
  assert.ok(slept >= 100);
});
