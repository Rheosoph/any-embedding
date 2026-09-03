import {
  HttpError,
  clientIp,
  errorResponse,
  parseJsonBody,
  requestId,
  requestMethod,
  requestPath,
  response,
  validateEmbeddingRequest,
  verifyApiKey,
} from "../contract.js";
import type { MicrovmService, PoolRegistryService } from "../dependencies.js";
import { errorMessage } from "../errors.js";
import { predictRuntimeSeconds, selectTier } from "../routing.js";
import type {
  GatewayHandler,
  HttpRequestEvent,
  HttpResponse,
  InstanceRecord,
  LambdaContext,
  ModelConfig,
  StreamingHttpResponse,
  TierSelection,
} from "../types.js";

type Sleep = (milliseconds: number) => Promise<void>;

export interface GatewayOptions {
  readonly registry: PoolRegistryService;
  readonly microvms: Pick<MicrovmService, "invoke">;
  readonly apiKeySha256?: string;
  readonly rateLimitRpm?: number;
  readonly sleep?: Sleep;
}

function publicModel(model: ModelConfig): {
  readonly id: string;
  readonly object: "model";
  readonly owned_by: "any-embedding";
  readonly permissions: readonly never[];
} {
  return {
    id: model.name,
    object: "model",
    owned_by: "any-embedding",
    permissions: [],
  };
}

function retryableUpstreamStatus(status: number): boolean {
  return status === 429 || status === 500;
}

async function readUpstreamError(upstream: Response): Promise<HttpResponse> {
  const text = await upstream.text();
  return response(upstream.status, { detail: text });
}

export function createGateway({
  registry,
  microvms,
  apiKeySha256 = "",
  rateLimitRpm = 300,
  sleep = async (milliseconds: number) => {
    await new Promise<void>((resolve) => setTimeout(resolve, milliseconds));
  },
}: GatewayOptions): GatewayHandler {
  if (!registry || !microvms) throw new Error("registry and microvms are required");
  const requestLog = new Map<string, number[]>();

  function checkRateLimit(ip: string, nowMilliseconds = Date.now()): void {
    const cutoff = nowMilliseconds - 60_000;
    const active = (requestLog.get(ip) ?? []).filter((timestamp) => timestamp > cutoff);
    if (active.length >= rateLimitRpm) {
      throw new HttpError(429, "Rate limit exceeded", { "retry-after": "60" });
    }
    active.push(nowMilliseconds);
    requestLog.set(ip, active);
  }

  async function acquire(
    model: ModelConfig,
    selection: TierSelection,
    leaseRequestId: string,
    predictedRuntime: number,
  ): Promise<InstanceRecord> {
    let instance = await registry.acquireCompatible(
      model,
      selection.tiers,
      selection.selectedIndex,
      leaseRequestId,
      predictedRuntime,
    );
    if (instance) return instance;

    instance = await registry.launchAndClaim(
      model,
      selection.selected,
      leaseRequestId,
      predictedRuntime,
    );
    if (instance) return instance;

    instance = await registry.waitForCapacity(
      model,
      selection.tiers,
      selection.selectedIndex,
      leaseRequestId,
      predictedRuntime,
    );
    if (instance) return instance;

    throw new HttpError(
      429,
      `All '${model.name}' ${selection.selected.name} pool capacity is busy`,
      { "retry-after": "1" },
    );
  }

  async function invokeEmbedding(
    event: HttpRequestEvent,
    context: LambdaContext | undefined,
  ): Promise<HttpResponse | StreamingHttpResponse> {
    const parsed = parseJsonBody(event);
    const validated = validateEmbeddingRequest(parsed);
    // FastAPI validates the request body before entering the GCP handler. Do
    // the same here so malformed requests do not consume the per-IP budget.
    checkRateLimit(clientIp(event));
    const model = await registry.getModel(validated.payload.model);
    if (!model) {
      const available = (await registry.listModels()).map((item) => item.name);
      throw new HttpError(
        400,
        `Model '${validated.payload.model}' not found. Available: [`
        + `${available.map((name) => `'${name}'`).join(", ")}]`,
      );
    }

    const selection = selectTier(model, validated.items);
    const predictedRuntime = predictRuntimeSeconds(selection.selected, selection.metrics);
    const baseRequestId = requestId(event);
    const requestBody = JSON.stringify(validated.payload);

    for (let attempt = 0; attempt < 2; attempt += 1) {
      const leaseRequestId = attempt === 0 ? baseRequestId : `${baseRequestId}:retry`;
      let instance: InstanceRecord | undefined;
      try {
        instance = await acquire(model, selection, leaseRequestId, predictedRuntime);
        let token = await registry.ensureAuthToken(instance, leaseRequestId);
        const remainingMilliseconds = context?.getRemainingTimeInMillis() ?? 300_000;
        const controller = new AbortController();
        const abortTimer = setTimeout(
          () => controller.abort(new Error("MicroVM request exceeded Lambda deadline")),
          Math.max(1_000, remainingMilliseconds - 2_000),
        );

        let upstream: Response;
        try {
          upstream = await microvms.invoke(instance, token, requestBody, {
            signal: controller.signal,
          });
          if (upstream.status === 403) {
            await upstream.body?.cancel().catch(() => undefined);
            token = await registry.ensureAuthToken(instance, leaseRequestId, { force: true });
            upstream = await microvms.invoke(instance, token, requestBody, {
              signal: controller.signal,
            });
          }
          if (retryableUpstreamStatus(upstream.status)) {
            await upstream.body?.cancel().catch(() => undefined);
            await sleep(100 + (attempt * 150));
            upstream = await microvms.invoke(instance, token, requestBody, {
              signal: controller.signal,
            });
          }
        } catch (error) {
          clearTimeout(abortTimer);
          throw error;
        }

        if (upstream.status === 404 || upstream.status === 502 || upstream.status === 503) {
          const failureText = await upstream.text().catch(() => `upstream ${upstream.status}`);
          clearTimeout(abortTimer);
          await registry.fail(instance, leaseRequestId, failureText);
          instance = undefined;
          if (attempt === 0) continue;
          throw new HttpError(503, "Embedding worker is unavailable", { "retry-after": "1" });
        }

        console.info(JSON.stringify({
          event: "embedding_routed",
          request_id: baseRequestId,
          model: model.name,
          selected_tier: selection.selected.name,
          actual_tier: instance.tier,
          microvm_id: instance.microvm_id,
          batch_items: selection.metrics.batchItems,
          max_estimated_tokens: selection.metrics.maxTokens,
          total_estimated_tokens: selection.metrics.totalTokens,
          predicted_response_bytes: selection.metrics.predictedResponseBytes,
          status: upstream.status,
        }));

        if (upstream.status !== 200) {
          const result = await readUpstreamError(upstream);
          clearTimeout(abortTimer);
          const leasedInstance = instance;
          await registry.release(leasedInstance, leaseRequestId).catch((error: unknown) => {
            console.error(JSON.stringify({
              event: "lease_release_failed",
              request_id: baseRequestId,
              microvm_id: leasedInstance.microvm_id,
              error: errorMessage(error),
            }));
          });
          return result;
        }

        if (!upstream.body) {
          clearTimeout(abortTimer);
          throw new Error("Embedding worker returned an empty response stream");
        }
        const leasedInstance = instance;
        return {
          statusCode: 200,
          headers: {
            "content-type": upstream.headers.get("content-type") ?? "application/json",
          },
          stream: upstream.body,
          cleanup: async () => {
            clearTimeout(abortTimer);
            await registry.release(leasedInstance, leaseRequestId);
          },
        };
      } catch (error) {
        // State-store, token-cache, and transient fetch failures are not proof
        // that the worker is unhealthy. Return its lease and let explicit
        // endpoint 404/502/503 handling above fence and terminate bad VMs.
        if (instance) {
          const leasedInstance = instance;
          await registry.release(leasedInstance, leaseRequestId).catch((releaseError: unknown) => {
            console.error(JSON.stringify({
              event: "lease_release_failed_after_error",
              request_id: baseRequestId,
              microvm_id: leasedInstance.microvm_id,
              error: errorMessage(releaseError),
            }));
          });
        }
        if (error instanceof HttpError || attempt === 1) throw error;
      }
    }
    throw new HttpError(503, "Embedding worker is unavailable", { "retry-after": "1" });
  }

  return async function handle(
    event: HttpRequestEvent,
    context?: LambdaContext,
  ): Promise<HttpResponse | StreamingHttpResponse> {
    try {
      const method = requestMethod(event);
      const path = requestPath(event);
      if (method === "GET" && path === "/health") {
        return response(200, { status: "ok", model: null });
      }
      if (method === "GET" && path === "/v1/models") {
        verifyApiKey(event, apiKeySha256);
        const models = await registry.listModels();
        return response(200, { object: "list", data: models.map(publicModel) });
      }
      if (method === "POST" && path === "/v1/embeddings") {
        verifyApiKey(event, apiKeySha256);
        return await invokeEmbedding(event, context);
      }
      const allowedMethod = new Map<string, string>([
        ["/health", "GET"],
        ["/v1/models", "GET"],
        ["/v1/embeddings", "POST"],
      ]).get(path);
      if (allowedMethod) {
        throw new HttpError(405, "Method Not Allowed", { allow: allowedMethod });
      }
      throw new HttpError(404, "Not Found");
    } catch (error) {
      return errorResponse(error);
    }
  };
}
