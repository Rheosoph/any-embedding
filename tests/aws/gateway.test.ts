import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { createGateway } from "../../app/aws/gateway/app.js";
import type {
  MicrovmService,
  PoolRegistryService,
} from "../../app/aws/dependencies.js";
import type {
  GatewayResponse,
  HttpRequestEvent,
  HttpResponse,
  InstanceRecord,
  ModelConfig,
  StreamingHttpResponse,
  TierConfig,
} from "../../app/aws/types.js";

const apiHash = createHash("sha256").update("test-key").digest("hex");
const tier = {
  name: "tier-1",
  order: 1,
  max_item_tokens: 512,
  max_total_tokens: 4096,
  max_batch_items: 64,
  max_attention_score: 64 * 512 * 512,
  max_response_bytes: 200_000_000,
} satisfies TierConfig;
const model = {
  name: "gte-multilingual-base",
  type: "text",
  dimensions: 768,
  max_tokens: 8192,
  order: 1,
  tiers: [tier],
} satisfies ModelConfig;

function event(
  method: string,
  path: string,
  body?: unknown,
  authorization: string | null = "Bearer test-key",
): HttpRequestEvent {
  return {
    rawPath: path,
    headers: authorization === null ? {} : { authorization },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    requestContext: {
      requestId: "11111111-1111-4111-8111-111111111111",
      http: { method, path, sourceIp: "127.0.0.1" },
    },
  };
}

function buffered(result: GatewayResponse): HttpResponse {
  if ("stream" in result) assert.fail("Expected a buffered response");
  return result;
}

function streaming(result: GatewayResponse): StreamingHttpResponse {
  if (result.stream === undefined) assert.fail("Expected a streaming response");
  return result as StreamingHttpResponse;
}

function fixture() {
  const calls = { released: 0, failed: 0, invoked: 0 };
  const instance = {
    pool_key: "POOL#gte-multilingual-base#tier-1",
    record_key: "INSTANCE#microvm-test",
    model: model.name,
    tier: "tier-1",
    state: "READY",
    status: "AVAILABLE",
    in_flight: 0,
    capacity: 1,
    microvm_id: "microvm-test",
    endpoint: "example.invalid",
    soft_expires_at: 9_999_999_999,
    hard_expires_at: 9999999999,
    auth_token: "token",
    auth_expires_at: 9999999999,
  } satisfies InstanceRecord;
  const registry = {
    listModels: async () => [model],
    getModel: async (name: string) => (name === model.name ? model : undefined),
    acquireCompatible: async () => instance,
    launchAndClaim: async () => undefined,
    waitForCapacity: async () => undefined,
    ensureAuthToken: async () => "token",
    release: async () => { calls.released += 1; },
    fail: async () => { calls.failed += 1; },
  } satisfies PoolRegistryService;
  const payload = {
    object: "list",
    data: [{ object: "embedding", embedding: [0.1, 0.2], index: 0 }],
    model: model.name,
    usage: { prompt_tokens: 1, total_tokens: 1 },
  };
  const microvms = {
    invoke: async () => {
      calls.invoked += 1;
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  } satisfies Pick<MicrovmService, "invoke">;
  return {
    gateway: createGateway({ registry, microvms, apiKeySha256: apiHash }),
    calls,
    payload,
  };
}

test("health is unauthenticated and matches the GCP shape", async () => {
  const { gateway } = fixture();
  const result = buffered(await gateway(event("GET", "/health", undefined, null)));
  assert.equal(result.statusCode, 200);
  assert.deepEqual(JSON.parse(result.body), { status: "ok", model: null });
});

test("models route requires auth and matches the GCP list shape", async () => {
  const { gateway } = fixture();
  const unauthorized = buffered(
    await gateway(event("GET", "/v1/models", undefined, null)),
  );
  assert.equal(unauthorized.statusCode, 401);
  assert.deepEqual(JSON.parse(unauthorized.body), { detail: "Missing Authorization header" });

  const result = buffered(await gateway(event("GET", "/v1/models")));
  assert.deepEqual(JSON.parse(result.body), {
    object: "list",
    data: [{
      id: model.name,
      object: "model",
      owned_by: "any-embedding",
      permissions: [],
    }],
  });
});

test("embedding response is streamed and lease releases after consumption", async () => {
  const { gateway, calls, payload } = fixture();
  const result = streaming(await gateway(event("POST", "/v1/embeddings", {
    model: model.name,
    input: "hello",
  })));
  assert.equal(result.statusCode, 200);
  assert.equal(calls.released, 0);
  const body = await new Response(result.stream).json();
  await result.cleanup();
  assert.deepEqual(body, payload);
  assert.equal(calls.invoked, 1);
  assert.equal(calls.released, 1);
  assert.equal(calls.failed, 0);
});

test("unknown models retain the public error envelope", async () => {
  const { gateway } = fixture();
  const result = buffered(await gateway(event("POST", "/v1/embeddings", {
    model: "missing",
    input: "hello",
  })));
  assert.equal(result.statusCode, 400);
  assert.match(JSON.parse(result.body).detail, /Model 'missing' not found/);
});

test("unknown paths and method mismatches match FastAPI before authentication", async () => {
  const { gateway } = fixture();
  const missing = buffered(await gateway(event("GET", "/missing", undefined, null)));
  assert.equal(missing.statusCode, 404);
  assert.deepEqual(JSON.parse(missing.body), { detail: "Not Found" });

  const wrongMethod = buffered(await gateway(event("POST", "/health", {}, null)));
  assert.equal(wrongMethod.statusCode, 405);
  assert.equal(wrongMethod.headers.allow, "GET");
  assert.deepEqual(JSON.parse(wrongMethod.body), { detail: "Method Not Allowed" });
});
