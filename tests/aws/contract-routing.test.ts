import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  HttpError,
  parseJsonBody,
  validateEmbeddingRequest,
  verifyApiKey,
} from "../../app/aws/contract.js";
import {
  estimateTextTokens,
  selectTier,
  workloadMetrics,
} from "../../app/aws/routing.js";
import type { ModelConfig, NormalizedEmbeddingInput } from "../../app/aws/types.js";

const apiHash = createHash("sha256").update("test-key").digest("hex");
const model = {
  name: "gte-multilingual-base",
  type: "text",
  dimensions: 768,
  max_tokens: 8192,
  tiers: [
    {
      name: "tier-1",
      order: 1,
      max_item_tokens: 512,
      max_total_tokens: 4096,
      max_batch_items: 64,
      max_attention_score: 64 * 512 * 512,
      max_response_bytes: 200_000_000,
    },
    {
      name: "tier-2",
      order: 2,
      max_item_tokens: 2048,
      max_total_tokens: 16384,
      max_batch_items: 256,
      max_attention_score: 32 * 2048 * 2048,
      max_response_bytes: 200_000_000,
    },
    {
      name: "tier-3",
      order: 3,
      max_item_tokens: 8192,
      max_total_tokens: 250000,
      max_batch_items: 2048,
      max_attention_score: 2048 * 8192 * 8192,
      max_response_bytes: 200_000_000,
    },
  ],
} satisfies ModelConfig;

function hasHttpStatus(statusCode: number): (error: unknown) => boolean {
  return (error: unknown): boolean => (
    error instanceof HttpError && error.statusCode === statusCode
  );
}

test("API key accepts the existing Bearer and raw forms", () => {
  assert.doesNotThrow(() => verifyApiKey({ headers: { authorization: "Bearer test-key" } }, apiHash));
  assert.doesNotThrow(() => verifyApiKey({ headers: { Authorization: " test-key " } }, apiHash));
  assert.throws(
    () => verifyApiKey({ headers: { authorization: "bearer test-key" } }, apiHash),
    hasHttpStatus(401),
  );
});

test("request validation preserves flexible text inputs and defaults encoding", () => {
  const event = {
    body: JSON.stringify({
      model: model.name,
      input: ["hello", { type: "text", text: "world" }],
    }),
  };
  const result = validateEmbeddingRequest(parseJsonBody(event));
  assert.equal(result.payload.encoding_format, "float");
  assert.deepEqual(
    result.items.map((item) => item.type === "text" ? item.text : assert.fail("Expected text")),
    ["hello", "world"],
  );
});

test("request validation keeps the existing empty-batch behavior", () => {
  const result = validateEmbeddingRequest({ model: model.name, input: [] });
  assert.deepEqual(result.items, []);
});

test("request validation rejects oversized batches and strings", () => {
  assert.throws(
    () => validateEmbeddingRequest({ model: model.name, input: Array(2049).fill("x") }),
    hasHttpStatus(422),
  );
  assert.throws(
    () => validateEmbeddingRequest({ model: model.name, input: "x".repeat(100001) }),
    hasHttpStatus(422),
  );
});

test("multilingual token estimate does not undercount unspaced CJK", () => {
  assert.equal(estimateTextTokens("你好世界"), 4);
  assert.ok(estimateTextTokens("The quick brown fox") >= 6);
});

test("routing selects the three measured capacity tiers", () => {
  assert.equal(selectTier(model, [{ type: "text", text: "short input" }]).selected.name, "tier-1");
  assert.equal(
    selectTier(model, [{ type: "text", text: "token ".repeat(900) }]).selected.name,
    "tier-2",
  );
  assert.equal(
    selectTier(model, [{ type: "text", text: "token ".repeat(2400) }]).selected.name,
    "tier-3",
  );
});

test("routing preserves single-input truncation but rejects unsafe aggregate work", () => {
  assert.equal(
    selectTier(model, [{ type: "text", text: "x".repeat(30_000) }]).selected.name,
    "tier-3",
  );
  assert.throws(
    () => selectTier(
      model,
      Array<NormalizedEmbeddingInput>(100).fill({
        type: "text",
        text: "x".repeat(30_000),
      }),
    ),
    hasHttpStatus(413),
  );
});

test("workload metrics include batch, quadratic attention, and response size", () => {
  const metrics = workloadMetrics([
    { type: "text", text: "one two" },
    { type: "text", text: "three four" },
  ], 768);
  assert.equal(metrics.batchItems, 2);
  assert.ok(metrics.attentionScore > 0);
  assert.equal(metrics.predictedResponseBytes, 2 * ((768 * 22) + 512));
});
