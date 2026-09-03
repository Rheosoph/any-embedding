import type { AttributeValue } from "@aws-sdk/client-dynamodb";
import type { Context } from "aws-lambda";

export type HttpHeaders = Record<string, string>;

export interface HttpRequestEvent {
  readonly rawPath?: string;
  readonly httpMethod?: string;
  readonly headers?: Record<string, string | undefined> | null;
  readonly body?: string | null;
  readonly isBase64Encoded?: boolean;
  readonly requestContext?: {
    readonly requestId?: string;
    readonly http?: {
      readonly method?: string;
      readonly path?: string;
      readonly requestId?: string;
      readonly sourceIp?: string;
    };
    readonly identity?: {
      readonly sourceIp?: string;
    };
  };
}

export type LambdaContext = Pick<Context, "getRemainingTimeInMillis">;

export interface HttpResponse {
  readonly statusCode: number;
  readonly headers: HttpHeaders;
  readonly body: string;
  readonly stream?: never;
  readonly cleanup?: never;
}

export interface StreamingHttpResponse {
  readonly statusCode: number;
  readonly headers: HttpHeaders;
  readonly stream: ReadableStream<Uint8Array>;
  readonly cleanup: () => Promise<void>;
  readonly body?: never;
}

export type GatewayResponse = HttpResponse | StreamingHttpResponse;
export type GatewayHandler = (
  event: HttpRequestEvent,
  context?: LambdaContext,
) => Promise<GatewayResponse>;

export interface ValidationIssue {
  readonly type: string;
  readonly loc: readonly (string | number)[];
  readonly msg: string;
  readonly input: unknown;
}

export type ErrorDetail = string | readonly ValidationIssue[];

export interface TextEmbeddingInput {
  readonly type: "text";
  readonly text: string;
}

export interface ImageEmbeddingInput {
  readonly type: "image";
  readonly image: unknown;
}

export type NormalizedEmbeddingInput = TextEmbeddingInput | ImageEmbeddingInput;

export interface EmbeddingRequestPayload extends Record<string, unknown> {
  readonly model: string;
  readonly input: unknown;
  readonly encoding_format: string;
}

export interface ValidatedEmbeddingRequest {
  readonly payload: EmbeddingRequestPayload;
  readonly items: readonly NormalizedEmbeddingInput[];
}

export interface TierConfig {
  readonly name: string;
  readonly order: number;
  readonly max_item_tokens: number;
  readonly max_total_tokens: number;
  readonly max_batch_items: number;
  readonly max_attention_score: number;
  readonly max_response_bytes: number;
}

export interface ModelConfig {
  readonly name: string;
  readonly type: string;
  readonly dimensions: number;
  readonly max_tokens?: number;
  readonly order?: number;
  readonly tiers: readonly TierConfig[];
}

export interface WorkloadMetrics {
  readonly batchItems: number;
  readonly maxTokens: number;
  readonly totalTokens: number;
  readonly attentionScore: number;
  readonly hasImage: boolean;
  readonly predictedResponseBytes: number;
}

export interface TierSelection {
  readonly tiers: readonly TierConfig[];
  readonly selectedIndex: number;
  readonly selected: TierConfig;
  readonly metrics: WorkloadMetrics;
}

export interface RecordKey {
  readonly pool_key: string;
  readonly record_key: string;
  readonly [key: string]: unknown;
}

export interface PoolConfigRecord extends RecordKey {
  readonly entity: "POOL";
  readonly model?: string;
  readonly tier?: string;
  readonly order?: number;
  readonly image_arn: string;
  readonly image_version: string;
  readonly max_instances: number;
  readonly capacity?: number;
  readonly idle_seconds?: number;
  readonly suspended_seconds?: number;
}

export interface MicrovmLaunchConfig {
  readonly pool_key: string;
  readonly image_arn: string;
  readonly image_version: string;
  readonly idle_seconds?: number;
  readonly suspended_seconds?: number;
}

export type InstanceState = "READY" | "FAILED" | string;
export type InstanceStatus = "AVAILABLE" | "BUSY" | "UNAVAILABLE" | string;

export interface InstanceRecord extends RecordKey {
  readonly entity?: "INSTANCE";
  readonly model: string;
  readonly tier: string;
  readonly tier_order?: number;
  readonly state: InstanceState;
  readonly status: InstanceStatus;
  readonly in_flight: number;
  readonly capacity: number;
  readonly microvm_id: string;
  readonly endpoint: string;
  readonly image_arn?: string;
  readonly image_version?: string;
  auth_token?: string;
  auth_expires_at?: number;
  readonly created_at?: number;
  readonly ready_at?: number;
  readonly last_used_at?: number;
  readonly last_completed_at?: number;
  readonly soft_expires_at: number;
  readonly hard_expires_at: number;
  readonly ttl_at?: number;
  readonly failure_reason?: string;
}

export interface LeaseRecord extends RecordKey {
  readonly entity: "LEASE";
  readonly request_id: string;
  readonly microvm_id: string;
  readonly lease_expires_at: number;
  readonly ttl_at: number;
}

export interface ReservationRecord extends RecordKey {
  readonly entity: "RESERVATION";
  readonly status?: string;
  readonly request_id?: string;
  readonly microvm_id?: string;
  readonly endpoint?: string;
  readonly image_arn?: string;
  readonly image_version?: string;
  readonly launch_token?: string;
  readonly created_at?: number;
  readonly ttl_at: number;
}

export interface ModelRecord extends ModelConfig, RecordKey {
  readonly entity: "MODEL";
}

export type StateRecord =
  | InstanceRecord
  | LeaseRecord
  | ModelRecord
  | PoolConfigRecord
  | ReservationRecord
  | (RecordKey & Record<string, unknown>);

export type DatabaseRecord = RecordKey & Record<string, unknown>;

export interface AuthToken {
  readonly token: string;
  readonly expiresAt: number;
}

export interface LaunchedMicrovm {
  readonly microvmId: string;
  readonly endpoint: string;
  readonly state?: string;
  readonly stateReason?: string;
  readonly imageArn?: string;
  readonly imageVersion?: string;
  readonly executionRoleArn?: string;
  readonly startedAt?: Date;
}

export interface MicrovmListItem {
  readonly microvmId: string;
  readonly state?: string;
  readonly imageArn?: string;
  readonly imageVersion?: string;
  readonly startedAt?: Date;
}

export interface MicrovmListPage {
  readonly items?: readonly MicrovmListItem[];
  readonly nextToken?: string;
}

export interface DynamoStreamRecord {
  readonly eventID?: string;
  readonly eventName?: string;
  readonly userIdentity?: {
    readonly type?: string;
    readonly principalId?: string;
  };
  readonly dynamodb?: {
    readonly OldImage?: Record<string, AttributeValue>;
    readonly SequenceNumber?: string;
  };
}

export interface DynamoStreamEvent {
  readonly Records: readonly DynamoStreamRecord[];
}

export interface ReconcileEvent {
  readonly source?: string;
  readonly action?: string;
}

export type LifecycleEvent = DynamoStreamEvent | ReconcileEvent;

export interface BatchItemFailure {
  readonly itemIdentifier: string;
}

export interface BatchItemFailureResponse {
  readonly batchItemFailures: readonly BatchItemFailure[];
}

export interface ReconcileSummary {
  scanned: number;
  terminated: number;
  recoveredLeases: number;
  removedReservations: number;
  terminatedOrphans: number;
  errors: number;
}
