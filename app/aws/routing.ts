import { HttpError } from "./contract.js";
import type {
  ModelConfig,
  NormalizedEmbeddingInput,
  TierConfig,
  TierSelection,
  WorkloadMetrics,
} from "./types.js";

const CJK_PATTERN = /[\u2e80-\u2fff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/gu;

export function estimateTextTokens(text: string): number {
  if (text.length === 0) return 0;
  const utf8Bytes = Buffer.byteLength(text, "utf8");
  const words = text.trim() === "" ? 0 : text.trim().split(/\s+/u).length;
  const cjkCharacters = (text.match(CJK_PATTERN) ?? []).length;
  // Deliberately conservative. The gateway stays tokenizer-free, while the
  // multilingual term prevents a byte/word heuristic from under-routing CJK.
  return Math.max(
    1,
    Math.ceil(utf8Bytes / 3),
    Math.ceil(words * 1.5),
    cjkCharacters,
  );
}

export function workloadMetrics(
  items: readonly NormalizedEmbeddingInput[],
  dimensions: number,
): WorkloadMetrics {
  const tokenCounts = items.map((item) => (
    item.type === "text" ? estimateTextTokens(item.text) : Number.MAX_SAFE_INTEGER
  ));
  const finiteCounts = tokenCounts.filter(Number.isFinite);
  const maxTokens = finiteCounts.length === 0 ? 0 : Math.max(...finiteCounts);
  const totalTokens = finiteCounts.reduce((total, count) => total + count, 0);
  const attentionScore = finiteCounts.reduce(
    (total, count) => total + (count * count),
    0,
  );
  return {
    batchItems: items.length,
    maxTokens,
    totalTokens,
    attentionScore,
    hasImage: items.some((item) => item.type === "image"),
    // Measured float JSON was about 21 bytes/dimension for this model. Keep
    // headroom for per-item metadata and numeric variation when enforcing
    // routing and Lambda's streaming response ceiling.
    predictedResponseBytes: items.length * ((dimensions * 22) + 512),
  };
}

function tierAccepts(tier: TierConfig, metrics: WorkloadMetrics): boolean {
  return (
    metrics.maxTokens <= tier.max_item_tokens
    && metrics.totalTokens <= tier.max_total_tokens
    && metrics.batchItems <= tier.max_batch_items
    && metrics.attentionScore <= tier.max_attention_score
    && metrics.predictedResponseBytes <= tier.max_response_bytes
  );
}

function largestTierCanSafelyRun(tier: TierConfig, metrics: WorkloadMetrics): boolean {
  // Token estimation is deliberately conservative, so a single input that is
  // estimated above max_item_tokens still goes to the largest worker and is
  // truncated by the pinned model just like GCP. Aggregate limits remain hard
  // guards: ignoring those can leave a CPU-bound encode running after both the
  // HTTP waiter and gateway have timed out.
  return (
    metrics.totalTokens <= tier.max_total_tokens
    && metrics.batchItems <= tier.max_batch_items
    && metrics.attentionScore <= tier.max_attention_score
    && metrics.predictedResponseBytes <= tier.max_response_bytes
  );
}

export function selectTier(
  model: ModelConfig,
  items: readonly NormalizedEmbeddingInput[],
): TierSelection {
  if (items.some((item) => item.type === "image") && model.type !== "image") {
    throw new HttpError(422, "This model accepts text inputs only");
  }
  const metrics = workloadMetrics(items, model.dimensions);
  const tiers = [...model.tiers].sort((left, right) => left.order - right.order);
  if (tiers.length === 0) throw new HttpError(503, `Model '${model.name}' has no AWS tiers`);

  const selectedIndex = tiers.findIndex((tier) => tierAccepts(tier, metrics));
  const largestTier = tiers.at(-1);
  if (!largestTier) throw new HttpError(503, `Model '${model.name}' has no AWS tiers`);
  // The public GCP contract truncates individual inputs at the model limit, so
  // do not reject only because the tokenizer-free max-item estimate is high.
  // Aggregate limits are operational safety bounds, however.
  if (selectedIndex < 0 && !largestTierCanSafelyRun(largestTier, metrics)) {
    throw new HttpError(413, "Embedding workload exceeds the AWS worker safety limit");
  }
  const effectiveIndex = selectedIndex < 0 ? tiers.length - 1 : selectedIndex;
  const selected = tiers[effectiveIndex];
  if (!selected) throw new Error("Tier selection produced an invalid index");
  return { tiers, selectedIndex: effectiveIndex, selected, metrics };
}

export function predictRuntimeSeconds(tier: TierConfig, metrics: WorkloadMetrics): number {
  const largest = metrics.maxTokens;
  const attentionUnits = metrics.attentionScore / (512 * 512);
  const tierFactor = Math.max(1, 4 / (2 ** (tier.order - 1)));
  return Math.min(
    240,
    Math.ceil(2 + (largest / 512) + (attentionUnits * tierFactor * 0.35)),
  );
}
