import {
  DeleteCommand,
  GetCommand,
  PutCommand,
  QueryCommand,
  ScanCommand,
  TransactWriteCommand,
  UpdateCommand,
  type DynamoDBDocumentClient,
} from "@aws-sdk/lib-dynamodb";
import {
  CreateMicrovmAuthTokenCommand,
  GetMicrovmCommand,
  ListMicrovmsCommand,
  RunMicrovmCommand,
  TerminateMicrovmCommand,
  type LambdaMicrovmsClient,
} from "@aws-sdk/client-lambda-microvms";

import type {
  AuthToken,
  InstanceRecord,
  LaunchedMicrovm,
  MicrovmLaunchConfig,
  MicrovmListPage,
  ModelConfig,
  TierConfig,
} from "./types.js";

export {
  CreateMicrovmAuthTokenCommand,
  DeleteCommand,
  GetCommand,
  GetMicrovmCommand,
  ListMicrovmsCommand,
  PutCommand,
  QueryCommand,
  RunMicrovmCommand,
  ScanCommand,
  TerminateMicrovmCommand,
  TransactWriteCommand,
  UpdateCommand,
};

export type DynamoCommand =
  | DeleteCommand
  | GetCommand
  | PutCommand
  | QueryCommand
  | ScanCommand
  | TransactWriteCommand
  | UpdateCommand;

export interface DynamoClient {
  send(command: DynamoCommand): Promise<unknown>;
}

export function adaptDynamoClient(client: DynamoDBDocumentClient): DynamoClient {
  return {
    send(command: DynamoCommand): Promise<unknown> {
      if (command instanceof DeleteCommand) return client.send(command);
      if (command instanceof GetCommand) return client.send(command);
      if (command instanceof PutCommand) return client.send(command);
      if (command instanceof QueryCommand) return client.send(command);
      if (command instanceof ScanCommand) return client.send(command);
      if (command instanceof TransactWriteCommand) return client.send(command);
      if (command instanceof UpdateCommand) return client.send(command);
      const exhaustive: never = command;
      throw new Error(`Unsupported DynamoDB command: ${String(exhaustive)}`);
    },
  };
}

export type MicrovmCommand =
  | CreateMicrovmAuthTokenCommand
  | GetMicrovmCommand
  | ListMicrovmsCommand
  | RunMicrovmCommand
  | TerminateMicrovmCommand;

export interface MicrovmSdkClient {
  send(command: MicrovmCommand): Promise<unknown>;
}

export function adaptMicrovmSdkClient(client: LambdaMicrovmsClient): MicrovmSdkClient {
  return {
    send(command: MicrovmCommand): Promise<unknown> {
      if (command instanceof CreateMicrovmAuthTokenCommand) return client.send(command);
      if (command instanceof GetMicrovmCommand) return client.send(command);
      if (command instanceof ListMicrovmsCommand) return client.send(command);
      if (command instanceof RunMicrovmCommand) return client.send(command);
      if (command instanceof TerminateMicrovmCommand) return client.send(command);
      const exhaustive: never = command;
      throw new Error(`Unsupported MicroVM command: ${String(exhaustive)}`);
    },
  };
}

export interface MicrovmService {
  launch(config: MicrovmLaunchConfig, clientToken: string): Promise<LaunchedMicrovm>;
  createAuthToken(microvmId: string, expirationInMinutes?: number): Promise<AuthToken>;
  waitUntilRunning(launched: LaunchedMicrovm): Promise<LaunchedMicrovm>;
  waitUntilReady(launched: LaunchedMicrovm, token: string): Promise<LaunchedMicrovm>;
  invoke(
    instance: InstanceRecord,
    token: string,
    body: string,
    options?: { readonly signal?: AbortSignal },
  ): Promise<Response>;
  get(microvmId: string): Promise<LaunchedMicrovm>;
  list(input?: {
    readonly imageIdentifier?: string;
    readonly imageVersion?: string;
    readonly nextToken?: string;
  }): Promise<MicrovmListPage>;
  terminate(microvmId: string): Promise<void>;
}

export interface PoolRegistryService {
  listModels(options?: { readonly force?: boolean }): Promise<readonly ModelConfig[]>;
  getModel(name: string): Promise<ModelConfig | undefined>;
  acquireCompatible(
    model: ModelConfig,
    tiers: readonly TierConfig[],
    firstTierIndex: number,
    requestId: string,
    predictedRuntimeSeconds: number,
  ): Promise<InstanceRecord | undefined>;
  launchAndClaim(
    model: ModelConfig,
    tier: TierConfig,
    requestId: string,
    predictedRuntimeSeconds?: number,
  ): Promise<InstanceRecord | undefined>;
  waitForCapacity(
    model: ModelConfig,
    tiers: readonly TierConfig[],
    firstTierIndex: number,
    requestId: string,
    predictedRuntimeSeconds: number,
  ): Promise<InstanceRecord | undefined>;
  ensureAuthToken(
    instance: InstanceRecord,
    requestId: string,
    options?: { readonly force?: boolean },
  ): Promise<string>;
  release(instance: InstanceRecord, requestId: string): Promise<void>;
  fail(instance: InstanceRecord, requestId: string, reason: string): Promise<void>;
}
