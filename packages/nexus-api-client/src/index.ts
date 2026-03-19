/**
 * @nexus/api-client — Shared HTTP client for Nexus APIs.
 *
 * @example
 * ```typescript
 * import { FetchClient, resolveConfig } from '@nexus/api-client';
 *
 * const config = resolveConfig({ apiKey: 'nx_live_myagent' });
 * const client = new FetchClient(config);
 * const files = await client.get('/api/v2/files/list?path=/');
 * ```
 *
 * @packageDocumentation
 */

// Client
export { FetchClient } from "./fetch-client.js";

// SSE
export { SseClient, RingBuffer } from "./sse-client.js";
export type { SseClientOptions, SseEventHandler, SseErrorHandler, SseReconnectHandler } from "./sse-client.js";

// Config
export { resolveConfig } from "./config.js";

// Errors
export {
  NexusApiError,
  AuthenticationError,
  ForbiddenError,
  NotFoundError,
  ConflictError,
  RateLimitError,
  ServerError,
  NetworkError,
  TimeoutError,
  AbortError,
} from "./errors.js";

// Types
export type {
  NexusClientOptions,
  RequestOptions,
  ApiErrorResponse,
  PaginatedResponse,
  SseEvent,
  AspectEnvelope,
  AspectListResponse,
  DatasetSchema,
  CatalogSchemaResponse,
  ColumnSearchResult,
  ColumnSearchResponse,
  ReplayRecord,
  ReplayResponse,
} from "./types.js";

// Case transform utilities
export {
  snakeToCamel,
  camelToSnake,
  transformKeys,
  snakeToCamelKeys,
  camelToSnakeKeys,
} from "./case-transform.js";
