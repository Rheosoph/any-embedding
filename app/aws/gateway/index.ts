import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient } from "@aws-sdk/lib-dynamodb";
import { once } from "node:events";

import { adaptDynamoClient } from "../dependencies.js";
import {
  numberEnvironment,
  requiredEnvironment,
  sha256Environment,
} from "../env.js";
import { errorMessage } from "../errors.js";
import {
  requireLambdaStreamingRuntime,
  type StreamingLambdaHandler,
} from "../lambda-runtime.js";
import { createGateway } from "./app.js";
import { MicrovmController } from "./microvms.js";
import { PoolRegistry } from "./pool.js";

const sdkDocumentClient = DynamoDBDocumentClient.from(new DynamoDBClient({}), {
  marshallOptions: { removeUndefinedValues: true },
});
const documentClient = adaptDynamoClient(sdkDocumentClient);

const hardTtlSeconds = numberEnvironment(
  "MICROVM_MAX_DURATION_SECONDS",
  25_200,
  { integer: true, minimum: 1, maximum: 28_800 },
);
const tokenMinutes = numberEnvironment(
  "TOKEN_MINUTES",
  60,
  { integer: true, minimum: 1, maximum: 60 },
);
const tokenRefreshMinutes = numberEnvironment(
  "TOKEN_REFRESH_MINUTES",
  55,
  { integer: true, minimum: 1, maximum: 59 },
);
if (tokenRefreshMinutes >= tokenMinutes) {
  throw new Error("TOKEN_REFRESH_MINUTES must be lower than TOKEN_MINUTES");
}

const microvmController = new MicrovmController({
  region: requiredEnvironment("AWS_REGION"),
  idleSeconds: numberEnvironment(
    "MICROVM_IDLE_SECONDS",
    300,
    { integer: true, minimum: 1 },
  ),
  suspendedSeconds: numberEnvironment(
    "MICROVM_SUSPENDED_SECONDS",
    1_500,
    { integer: true, minimum: 1 },
  ),
  maximumDurationSeconds: hardTtlSeconds,
  executionRoleArn: requiredEnvironment("MICROVM_EXECUTION_ROLE_ARN"),
  logGroup: requiredEnvironment("MICROVM_LOG_GROUP"),
  ingressConnectorArn: requiredEnvironment("MICROVM_INGRESS_CONNECTOR_ARN"),
  egressConnectorArn: requiredEnvironment("MICROVM_EGRESS_CONNECTOR_ARN"),
  port: numberEnvironment(
    "MICROVM_PORT",
    8_080,
    { integer: true, minimum: 1, maximum: 65_535 },
  ),
  readyTimeoutSeconds: numberEnvironment(
    "MICROVM_READY_TIMEOUT_SECONDS",
    120,
    { integer: true, minimum: 1 },
  ),
});
const registry = new PoolRegistry({
  ddb: documentClient,
  microvms: microvmController,
  tableName: requiredEnvironment("STATE_TABLE_NAME"),
  softTtlSeconds: numberEnvironment(
    "SOFT_TTL_SECONDS",
    1_800,
    { integer: true, minimum: 1 },
  ),
  hardTtlSeconds,
  leaseSeconds: numberEnvironment(
    "LEASE_SECONDS",
    900,
    { integer: true, minimum: 1 },
  ),
  tokenMinutes,
  tokenRefreshMinutes,
  launchLockSeconds: numberEnvironment(
    "LAUNCH_LOCK_SECONDS",
    180,
    { integer: true, minimum: 1 },
  ),
  hardExpiryHeadroomSeconds: numberEnvironment(
    "HARD_EXPIRY_HEADROOM_SECONDS",
    900,
    { integer: true, minimum: 1 },
  ),
});
export const handleRequest = createGateway({
  registry,
  microvms: microvmController,
  apiKeySha256: sha256Environment("API_KEY_SHA256"),
  rateLimitRpm: numberEnvironment(
    "RATE_LIMIT_RPM",
    300,
    { integer: true, minimum: 1 },
  ),
});

const runtime = requireLambdaStreamingRuntime();
const streamingHandler: StreamingLambdaHandler = async (event, responseStream, context) => {
  const result = await handleRequest(event, context);
  const output = runtime.HttpResponseStream.from(responseStream, {
    statusCode: result.statusCode,
    headers: result.headers,
  });
  try {
    if ("stream" in result) {
      for await (const chunk of result.stream) {
        if (!output.write(chunk)) await once(output, "drain");
      }
    } else {
      output.write(result.body);
    }
    output.end();
  } finally {
    if ("cleanup" in result) {
      await result.cleanup().catch((error: unknown) => {
        console.error(JSON.stringify({
          event: "lease_release_failed",
          error: errorMessage(error),
        }));
      });
    }
  }
};

export const handler = runtime.streamifyResponse(streamingHandler);
