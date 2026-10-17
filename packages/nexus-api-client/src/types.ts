/**
 * Shared types for the Nexus API client.
 */

export interface NexusClientOptions {
  /** API key (e.g. `nx_live_<id>`, `nx_test_<id>`, or any bearer token). */
  readonly apiKey: string;

  /** Base URL of the Nexus API server. Default: "http://localhost:2026" */
  readonly baseUrl?: string;

  /** Owning Rust Search host's HTTP URL. Defaults to baseUrl. */
  readonly searchUrl?: string;

  /** Request timeout in milliseconds. Default: 30000 */
  readonly timeout?: number;

  /** Maximum retries for retryable errors. Default: 3. Set to 0 to disable. */
  readonly maxRetries?: number;

  /** Custom fetch implementation for testing or proxying. */
  readonly fetch?: typeof globalThis.fetch;

  /** Disable automatic snake_case → camelCase key transformation. Default: true */
  readonly transformKeys?: boolean;

  /** Agent identity sent as X-Agent-ID header. */
  readonly agentId?: string;

  /** Subject identity sent as X-Nexus-Subject header. */
  readonly subject?: string;

  /** Zone ID sent as X-Nexus-Zone-ID header. */
  readonly zoneId?: string;
}

export interface RequestOptions {
  /** Query fields, encoded once and overriding the same fields in the path. */
  readonly query?: Readonly<Record<string, string | number | boolean | undefined>>;
  /** Explicit destination override for a co-owned API listener. */
  readonly baseUrl?: string;
  /** Override key transformation for an API whose wire types are used directly. */
  readonly transformKeys?: boolean;
  /** Per-request timeout override in milliseconds. */
  readonly timeout?: number;

  /** AbortSignal for cancellation. */
  readonly signal?: AbortSignal;

  /** Idempotency key for retry-safe operations. */
  readonly idempotencyKey?: string;

  /** Extra headers merged with defaults. */
  readonly headers?: Readonly<Record<string, string>>;
}

export interface ApiErrorResponse {
  readonly detail?: string;
  readonly error?: string;
  readonly error_code?: string;
}

/** Authenticated request context returned by GET /v2/auth/whoami. */
export interface NativeIdentity {
  readonly subject_type: string;
  readonly subject_id: string;
  readonly user_id: string;
  readonly agent_id: string | null;
  readonly zone_id: string;
  readonly is_admin: boolean;
  readonly zone_perms: readonly (readonly [string, string])[];
}

export interface PaginatedResponse<T> {
  readonly items: readonly T[];
  readonly total: number;
  readonly page: number;
  readonly limit: number;
}

export interface SseEvent {
  readonly id?: string;
  readonly event: string;
  readonly data: string;
  readonly retry?: number;
}

// =============================================================================
// Knowledge platform types (Issue #2930)
// =============================================================================

export interface AspectEnvelope {
  readonly entityUrn: string;
  readonly aspectName: string;
  readonly version: number;
  readonly payload: Record<string, unknown>;
  readonly createdBy: string;
  readonly createdAt: string | null;
}

export interface AspectListResponse {
  readonly entityUrn: string;
  readonly aspects: readonly string[];
}

export interface DatasetSchema {
  readonly columns: readonly { name: string; type: string; nullable: string }[];
  readonly format: string;
  readonly rowCount: number | null;
  readonly confidence: number;
  readonly warnings: readonly string[];
}

export interface CatalogSchemaResponse {
  readonly entityUrn: string;
  readonly path: string;
  readonly schema: DatasetSchema | null;
}

export interface ColumnSearchResult {
  readonly entityUrn: string;
  readonly columnName: string;
  readonly columnType: string;
  readonly schema: Record<string, unknown>;
}

export interface ColumnSearchResponse {
  readonly results: readonly ColumnSearchResult[];
  readonly total: number;
  readonly capped: boolean;
}

export interface ReplayRecord {
  readonly sequenceNumber: number;
  readonly entityUrn: string;
  readonly aspectName: string;
  readonly changeType: string;
  readonly timestamp: string;
  readonly operationType: string;
}

export interface ReplayResponse {
  readonly records: readonly ReplayRecord[];
  readonly nextCursor: number | null;
  readonly hasMore: boolean;
}
