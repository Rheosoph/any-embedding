import {
  CreateMicrovmAuthTokenCommand,
  GetMicrovmCommand,
  LambdaMicrovmsClient,
  ListMicrovmsCommand,
  RunMicrovmCommand,
  TerminateMicrovmCommand,
  type CreateMicrovmAuthTokenCommandOutput,
  type GetMicrovmCommandOutput,
  type ListMicrovmsCommandInput,
  type ListMicrovmsCommandOutput,
  type RunMicrovmCommandInput,
  type RunMicrovmCommandOutput,
} from "@aws-sdk/client-lambda-microvms";

import {
  adaptMicrovmSdkClient,
  type MicrovmCommand,
  type MicrovmSdkClient,
  type MicrovmService,
} from "../dependencies.js";
import { errorName } from "../errors.js";
import type {
  AuthToken,
  InstanceRecord,
  LaunchedMicrovm,
  MicrovmLaunchConfig,
  MicrovmListPage,
} from "../types.js";

type Sleep = (milliseconds: number) => Promise<void>;

export interface MicrovmControllerOptions {
  readonly client?: MicrovmSdkClient;
  readonly region?: string;
  readonly idleSeconds?: number;
  readonly suspendedSeconds?: number;
  readonly maximumDurationSeconds?: number;
  readonly executionRoleArn?: string;
  readonly logGroup?: string;
  readonly ingressConnectorArn?: string;
  readonly egressConnectorArn?: string;
  readonly port?: number;
  readonly fetchImpl?: typeof fetch;
  readonly sleep?: Sleep;
  readonly clock?: () => number;
  readonly nowMilliseconds?: () => number;
  readonly readyTimeoutSeconds?: number;
}

function defaultClient(): MicrovmSdkClient {
  return adaptMicrovmSdkClient(new LambdaMicrovmsClient({}));
}

function retryable(error: unknown): boolean {
  return [
    "InternalServerException",
    "ServiceUnavailableException",
    "ThrottlingException",
  ].includes(errorName(error) ?? "");
}

function requiredMicrovm(
  output: RunMicrovmCommandOutput | GetMicrovmCommandOutput,
  operation: string,
): LaunchedMicrovm {
  if (!output.microvmId || !output.endpoint) {
    throw new Error(`${operation} returned no MicroVM identifier or endpoint`);
  }
  return {
    microvmId: output.microvmId,
    endpoint: output.endpoint,
    ...(output.state ? { state: output.state } : {}),
    ...(output.stateReason ? { stateReason: output.stateReason } : {}),
    ...(output.imageArn ? { imageArn: output.imageArn } : {}),
    ...(output.imageVersion ? { imageVersion: output.imageVersion } : {}),
    ...(output.executionRoleArn ? { executionRoleArn: output.executionRoleArn } : {}),
    ...(output.startedAt ? { startedAt: output.startedAt } : {}),
  };
}

export class MicrovmController implements MicrovmService {
  readonly client: MicrovmSdkClient;
  readonly region: string;
  readonly idleSeconds: number;
  readonly suspendedSeconds: number;
  readonly maximumDurationSeconds: number;
  readonly executionRoleArn: string | undefined;
  readonly logGroup: string | undefined;
  readonly ingressConnectorArn: string | undefined;
  readonly egressConnectorArn: string | undefined;
  readonly port: number;
  readonly fetch: typeof fetch;
  readonly sleep: Sleep;
  readonly clock: () => number;
  readonly nowMilliseconds: () => number;
  readonly readyTimeoutSeconds: number;

  constructor({
    client = defaultClient(),
    region = "eu-west-1",
    idleSeconds = 300,
    suspendedSeconds = 1_500,
    maximumDurationSeconds = 25_200,
    executionRoleArn,
    logGroup,
    ingressConnectorArn,
    egressConnectorArn,
    port = 8_080,
    fetchImpl = globalThis.fetch,
    sleep = async (milliseconds: number) => {
      await new Promise<void>((resolve) => setTimeout(resolve, milliseconds));
    },
    clock = () => Math.floor(Date.now() / 1000),
    nowMilliseconds = () => Date.now(),
    readyTimeoutSeconds = 120,
  }: MicrovmControllerOptions = {}) {
    this.client = client;
    this.region = region;
    this.idleSeconds = idleSeconds;
    this.suspendedSeconds = suspendedSeconds;
    this.maximumDurationSeconds = maximumDurationSeconds;
    this.executionRoleArn = executionRoleArn;
    this.logGroup = logGroup;
    this.ingressConnectorArn = ingressConnectorArn;
    this.egressConnectorArn = egressConnectorArn;
    this.port = port;
    this.fetch = fetchImpl;
    this.sleep = sleep;
    this.clock = clock;
    this.nowMilliseconds = nowMilliseconds;
    this.readyTimeoutSeconds = readyTimeoutSeconds;
  }

  private async sendWithRetry<T>(
    commandFactory: () => MicrovmCommand,
    attempts = 3,
  ): Promise<T> {
    let lastError: unknown;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        return await this.client.send(commandFactory()) as T;
      } catch (error) {
        lastError = error;
        if (!retryable(error) || attempt === attempts - 1) throw error;
        await this.sleep((100 * (2 ** attempt)) + Math.floor(Math.random() * 75));
      }
    }
    throw lastError ?? new Error("MicroVM request exhausted its retry budget");
  }

  async launch(config: MicrovmLaunchConfig, clientToken: string): Promise<LaunchedMicrovm> {
    const ingress = this.ingressConnectorArn
      ?? `arn:aws:lambda:${this.region}:aws:network-connector:aws-network-connector:ALL_INGRESS`;
    const egress = this.egressConnectorArn
      ?? `arn:aws:lambda:${this.region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS`;
    const input: RunMicrovmCommandInput = {
      imageIdentifier: config.image_arn,
      imageVersion: config.image_version,
      ingressNetworkConnectors: [ingress],
      // The service applies INTERNET_EGRESS when this field is omitted or an
      // empty list is supplied. Send the effective connector explicitly so
      // deployed state and the inventory cannot imply false isolation.
      egressNetworkConnectors: [egress],
      idlePolicy: {
        autoResumeEnabled: true,
        maxIdleDurationSeconds: config.idle_seconds ?? this.idleSeconds,
        suspendedDurationSeconds: config.suspended_seconds ?? this.suspendedSeconds,
      },
      maximumDurationInSeconds: this.maximumDurationSeconds,
      clientToken,
      runHookPayload: JSON.stringify({
        deployment: "any-embedding-aws",
        pool_key: config.pool_key,
      }),
      logging: this.logGroup
        ? { cloudWatch: { logGroup: this.logGroup } }
        : { disabled: {} },
      ...(this.executionRoleArn ? { executionRoleArn: this.executionRoleArn } : {}),
    };
    const output = await this.sendWithRetry<RunMicrovmCommandOutput>(
      () => new RunMicrovmCommand(input),
    );
    return requiredMicrovm(output, "RunMicrovm");
  }

  async createAuthToken(microvmId: string, expirationInMinutes = 30): Promise<AuthToken> {
    const output = await this.sendWithRetry<CreateMicrovmAuthTokenCommandOutput>(
      () => new CreateMicrovmAuthTokenCommand({
        microvmIdentifier: microvmId,
        expirationInMinutes,
        allowedPorts: [{ port: this.port }],
      }),
    );
    const token = output.authToken?.["X-aws-proxy-auth"];
    if (!token) throw new Error("CreateMicrovmAuthToken returned no proxy token");
    return {
      token,
      expiresAt: this.clock() + (expirationInMinutes * 60),
    };
  }

  async waitUntilRunning(launched: LaunchedMicrovm): Promise<LaunchedMicrovm> {
    const deadline = this.nowMilliseconds() + (this.readyTimeoutSeconds * 1000);
    let lastState = launched.state;
    let attempt = 0;
    while (this.nowMilliseconds() < deadline) {
      try {
        const details = await this.get(launched.microvmId);
        lastState = details.state;
        if (lastState === "RUNNING") return { ...launched, ...details };
        if (lastState === "TERMINATING" || lastState === "TERMINATED") {
          throw new Error(
            `MicroVM ${launched.microvmId} became ${lastState}: `
            + `${details.stateReason ?? "unknown reason"}`,
          );
        }
      } catch (error) {
        if (errorName(error) !== "ResourceNotFoundException") throw error;
      }
      await this.sleep(Math.min(1_000, 100 * (2 ** Math.min(attempt, 3))));
      attempt += 1;
    }
    throw new Error(
      `MicroVM ${launched.microvmId} did not reach RUNNING within `
      + `${this.readyTimeoutSeconds}s (last state ${lastState ?? "unknown"})`,
    );
  }

  async waitUntilReady(launched: LaunchedMicrovm, token: string): Promise<LaunchedMicrovm> {
    const deadline = this.nowMilliseconds() + (this.readyTimeoutSeconds * 1000);
    let lastState = launched.state;
    let details = launched;
    let attempt = 0;

    while (this.nowMilliseconds() < deadline) {
      try {
        details = await this.get(launched.microvmId);
        lastState = details.state;
        if (lastState === "TERMINATING" || lastState === "TERMINATED") {
          throw new Error(
            `MicroVM ${launched.microvmId} became ${lastState}: `
            + `${details.stateReason ?? "unknown reason"}`,
          );
        }
      } catch (error) {
        if (errorName(error) !== "ResourceNotFoundException") throw error;
      }

      // GetMicrovm is eventually consistent. AWS explicitly recommends using
      // the endpoint as the readiness authority, so probe it even if the last
      // control-plane state still says PENDING.
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 3_000);
      try {
        const health = await this.fetch(`https://${launched.endpoint}/health`, {
          headers: {
            "x-aws-proxy-auth": token,
            "x-aws-proxy-port": String(this.port),
          },
          signal: controller.signal,
        });
        if (health.status === 200) {
          await health.body?.cancel().catch(() => undefined);
          return { ...launched, ...details, state: "RUNNING" };
        }
        await health.body?.cancel().catch(() => undefined);
      } catch {
        // Provisioning endpoints commonly refuse or return 502 before /run is
        // complete. The bounded loop below is the retry policy.
      } finally {
        clearTimeout(timer);
      }

      await this.sleep(Math.min(1_000, 150 * (2 ** Math.min(attempt, 3))));
      attempt += 1;
    }
    throw new Error(
      `MicroVM ${launched.microvmId} endpoint was not ready within `
      + `${this.readyTimeoutSeconds}s (last state ${lastState ?? "unknown"})`,
    );
  }

  async invoke(
    instance: InstanceRecord,
    token: string,
    body: string,
    { signal }: { readonly signal?: AbortSignal } = {},
  ): Promise<Response> {
    return this.fetch(`https://${instance.endpoint}/v1/embeddings`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-aws-proxy-auth": token,
        "x-aws-proxy-port": String(this.port),
      },
      body,
      ...(signal ? { signal } : {}),
    });
  }

  async get(microvmId: string): Promise<LaunchedMicrovm> {
    const output = await this.sendWithRetry<GetMicrovmCommandOutput>(
      () => new GetMicrovmCommand({ microvmIdentifier: microvmId }),
      3,
    );
    return requiredMicrovm(output, "GetMicrovm");
  }

  async list(input: ListMicrovmsCommandInput = {}): Promise<MicrovmListPage> {
    const output = await this.sendWithRetry<ListMicrovmsCommandOutput>(
      () => new ListMicrovmsCommand(input),
      3,
    );
    const items = (output.items ?? []).map((item) => {
      if (!item.microvmId) throw new Error("ListMicrovms returned an item without an identifier");
      return {
        microvmId: item.microvmId,
        ...(item.state ? { state: item.state } : {}),
        ...(item.imageArn ? { imageArn: item.imageArn } : {}),
        ...(item.imageVersion ? { imageVersion: item.imageVersion } : {}),
        ...(item.startedAt ? { startedAt: item.startedAt } : {}),
      };
    });
    return {
      items,
      ...(output.nextToken ? { nextToken: output.nextToken } : {}),
    };
  }

  async terminate(microvmId: string): Promise<void> {
    await this.sendWithRetry(
      () => new TerminateMicrovmCommand({ microvmIdentifier: microvmId }),
      3,
    );
  }
}
