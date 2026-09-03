import type { Writable } from "node:stream";

import type { HttpRequestEvent, LambdaContext } from "./types.js";

export interface LambdaResponseMetadata {
  readonly statusCode: number;
  readonly headers: Readonly<Record<string, string>>;
}

export type StreamingLambdaHandler = (
  event: HttpRequestEvent,
  responseStream: Writable,
  context: LambdaContext,
) => Promise<void>;

export type LambdaHandler = (
  event: HttpRequestEvent,
  context: LambdaContext,
) => Promise<void>;

export interface LambdaStreamingRuntime {
  readonly HttpResponseStream: {
    from(responseStream: Writable, metadata: LambdaResponseMetadata): Writable;
  };
  streamifyResponse(handler: StreamingLambdaHandler): LambdaHandler;
}

export function requireLambdaStreamingRuntime(): LambdaStreamingRuntime {
  const candidate: unknown = Reflect.get(globalThis, "awslambda");
  if (typeof candidate !== "object" || candidate === null
    || !(("HttpResponseStream" in candidate) && ("streamifyResponse" in candidate))) {
    throw new Error("AWS Lambda response-streaming runtime is unavailable");
  }
  return candidate as unknown as LambdaStreamingRuntime;
}
