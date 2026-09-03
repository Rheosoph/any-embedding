import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import { DynamoDBDocumentClient } from "@aws-sdk/lib-dynamodb";

import { adaptDynamoClient } from "../dependencies.js";
import { numberEnvironment, requiredEnvironment } from "../env.js";
import { MicrovmController } from "../gateway/microvms.js";
import { createLifecycleService } from "./service.js";

const sdkDocumentClient = DynamoDBDocumentClient.from(new DynamoDBClient({}), {
  marshallOptions: { removeUndefinedValues: true },
});
const documentClient = adaptDynamoClient(sdkDocumentClient);
const microvms = new MicrovmController({
  region: requiredEnvironment("AWS_REGION"),
  maximumDurationSeconds: numberEnvironment(
    "MICROVM_MAX_DURATION_SECONDS",
    25_200,
    { integer: true, minimum: 1, maximum: 28_800 },
  ),
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

const lifecycle = createLifecycleService({
  ddb: documentClient,
  microvms,
  tableName: requiredEnvironment("STATE_TABLE_NAME"),
  executionRoleArn: requiredEnvironment("MICROVM_EXECUTION_ROLE_ARN"),
  orphanGraceSeconds: numberEnvironment(
    "ORPHAN_GRACE_SECONDS",
    600,
    { integer: true, minimum: 0 },
  ),
});

export const handler = lifecycle.handler;
