import { createHash, randomUUID, timingSafeEqual } from "node:crypto";

import { errorMessage, errorName } from "./errors.js";
import type {
  EmbeddingRequestPayload,
  ErrorDetail,
  HttpHeaders,
  HttpRequestEvent,
  HttpResponse,
  NormalizedEmbeddingInput,
  ValidatedEmbeddingRequest,
  ValidationIssue,
} from "./types.js";

export const JSON_HEADERS = Object.freeze({
  "content-type": "application/json",
}) satisfies HttpHeaders;

export class HttpError extends Error {
  readonly statusCode: number;
  readonly detail: ErrorDetail;
  readonly headers: HttpHeaders;

  constructor(statusCode: number, detail: ErrorDetail, headers: HttpHeaders = {}) {
    super(typeof detail === "string" ? detail : "request failed");
    this.name = "HttpError";
    this.statusCode = statusCode;
    this.detail = detail;
    this.headers = headers;
  }
}

export function response(
  statusCode: number,
  payload: unknown,
  headers: HttpHeaders = {},
): HttpResponse {
  return {
    statusCode,
    headers: { ...JSON_HEADERS, ...headers },
    body: JSON.stringify(payload),
  };
}

export function errorResponse(error: unknown): HttpResponse {
  if (error instanceof HttpError) {
    return response(error.statusCode, { detail: error.detail }, error.headers);
  }
  console.error(JSON.stringify({
    event: "gateway_error",
    error_type: errorName(error) ?? "Error",
    error: errorMessage(error),
  }));
  return response(500, { detail: "Internal server error" });
}

export function requestPath(event: HttpRequestEvent): string {
  return event.rawPath ?? event.requestContext?.http?.path ?? "/";
}

export function requestMethod(event: HttpRequestEvent): string {
  return (event.requestContext?.http?.method ?? event.httpMethod ?? "GET").toUpperCase();
}

export function requestId(event: HttpRequestEvent): string {
  return event.requestContext?.requestId ?? event.requestContext?.http?.requestId ?? randomUUID();
}

function characterCount(value: string): number {
  // Python's len(str), used by the GCP/Pydantic endpoint, counts Unicode code
  // points. JavaScript's String.length counts UTF-16 code units instead.
  return [...value].length;
}

export function clientIp(event: HttpRequestEvent): string {
  return event.requestContext?.http?.sourceIp
    ?? event.requestContext?.identity?.sourceIp
    ?? "unknown";
}

export function header(event: HttpRequestEvent, name: string): string | undefined {
  const expected = name.toLowerCase();
  for (const [key, value] of Object.entries(event.headers ?? {})) {
    if (key.toLowerCase() === expected) return value;
  }
  return undefined;
}

export function verifyApiKey(event: HttpRequestEvent, expectedSha256: string): void {
  if (!expectedSha256) {
    throw new HttpError(500, "API_KEY not configured on server");
  }
  const authorization = header(event, "authorization");
  if (authorization === undefined) {
    throw new HttpError(401, "Missing Authorization header");
  }

  const value = authorization.startsWith("Bearer ")
    ? authorization.slice("Bearer ".length).trim()
    : authorization.trim();
  const actual = Buffer.from(createHash("sha256").update(value).digest("hex"), "utf8");
  const expected = Buffer.from(expectedSha256.toLowerCase(), "utf8");
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
    throw new HttpError(401, "Invalid API key");
  }
}

function validationError(
  location: readonly (string | number)[],
  message: string,
  input: unknown,
  type = "value_error",
): ValidationIssue {
  return { type, loc: ["body", ...location], msg: message, input };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseJsonBody(event: HttpRequestEvent): Record<string, unknown> {
  let raw = event.body;
  if (raw === undefined || raw === null || raw === "") {
    throw new HttpError(422, [
      validationError([], "Field required", null, "missing"),
    ]);
  }
  if (event.isBase64Encoded === true) raw = Buffer.from(raw, "base64").toString("utf8");
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!isObject(parsed)) {
      throw new HttpError(422, [
        validationError(
          [],
          "Input should be a valid dictionary or object",
          parsed,
          "model_attributes_type",
        ),
      ]);
    }
    return parsed;
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(422, [
      validationError([], "Invalid JSON body", raw, "json_invalid"),
    ]);
  }
}

export function validateEmbeddingRequest(
  payload: Record<string, unknown>,
): ValidatedEmbeddingRequest {
  const errors: ValidationIssue[] = [];
  if (!("input" in payload)) {
    errors.push(validationError(["input"], "Field required", payload, "missing"));
  }
  if (!("model" in payload)) {
    errors.push(validationError(["model"], "Field required", payload, "missing"));
  } else if (typeof payload.model !== "string") {
    errors.push(validationError(
      ["model"],
      "Input should be a valid string",
      payload.model,
      "string_type",
    ));
  }
  if ("encoding_format" in payload && typeof payload.encoding_format !== "string") {
    errors.push(validationError(
      ["encoding_format"],
      "Input should be a valid string",
      payload.encoding_format,
      "string_type",
    ));
  }
  if (errors.length > 0) throw new HttpError(422, errors);

  const rawInput = payload.input;
  const items = Array.isArray(rawInput) ? rawInput : [rawInput];
  if (items.length > 2048) {
    throw new HttpError(422, [
      validationError(
        ["input"],
        "Value error, Batch size must not exceed 2048 items",
        rawInput,
      ),
    ]);
  }

  const normalized: NormalizedEmbeddingInput[] = [];
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    if (typeof item === "string") {
      if (characterCount(item) > 100_000) {
        throw new HttpError(422, [
          validationError(
            ["input"],
            "Value error, Text input must not exceed 100,000 characters",
            rawInput,
          ),
        ]);
      }
      normalized.push({ type: "text", text: item });
      continue;
    }
    if (isObject(item)) {
      const type = item.type ?? "text";
      if (type === "text" && typeof item.text === "string") {
        if (characterCount(item.text) > 100_000) {
          throw new HttpError(422, [
            validationError(
              ["input"],
              "Value error, Text input must not exceed 100,000 characters",
              rawInput,
            ),
          ]);
        }
        normalized.push({ type: "text", text: item.text });
        continue;
      }
      if ((type === "image" || type === "image_url" || type === "image_base64") && item.image) {
        normalized.push({ type: "image", image: item.image });
        continue;
      }
    }
    throw new HttpError(422, [
      validationError(
        ["input", index],
        "Input should be a valid string or input object",
        item,
        "union_tag_invalid",
      ),
    ]);
  }

  const requestPayload: EmbeddingRequestPayload = {
    ...payload,
    model: payload.model as string,
    input: rawInput,
    encoding_format: typeof payload.encoding_format === "string"
      ? payload.encoding_format
      : "float",
  };
  return { payload: requestPayload, items: normalized };
}
