/**
 * Shared types for the Nexus API client.
 */

export interface NexusClientOptions {
  /** API key (e.g. `nx_live_<id>`, `nx_test_<id>`, or any bearer token). */
  readonly apiKey: string;

  /** Base URL of the Nexus API server. Default: "http://localhost:2026" */
  readonly baseUrl?: string;

  /** Request timeout in milliseconds. Default: 30000 */
  readonly timeout?: number;

  /** Maximum retries for retryable errors. Default: 3. Set to 0 to disable. */
  readonly maxRetries?: number;

  /** Custom fetch implementation for testing or proxying. */
  readonly fetch?: typeof globalThis.fetch;

  /** Disable automatic snake_case → camelCase key transformation. Default: true */
  readonly transformKeys?: boolean;
}

export interface RequestOptions {
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
  readonly detail: string;
  readonly error_code?: string;
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
