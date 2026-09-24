/**
 * @nexus-ai-fs/api-client — Shared HTTP client for Nexus APIs.
 *
 * @example
 * ```typescript
 * import { FetchClient, VfsClient, resolveConfig } from '@nexus-ai-fs/api-client';
 *
 * const config = resolveConfig({ apiKey: 'nx_live_myagent' });
 * const client = new FetchClient(config);
 * const files = await new VfsClient(client).list('/');
 * ```
 *
 * @packageDocumentation
 */

// Client
export { FetchClient } from "./fetch-client.js";
export { VfsClient } from "./vfs-client.js";
export type { FileMetadata, WriteReceipt } from "./vfs-client.js";
export { SearchClient } from "./search-client.js";
export type { SearchHit, SearchResponse, SearchQuery, SearchBatchResponse, GlobResponse, GrepResponse, GrepHit, SearchConnection, DiscoveryOptions, SearchHealth, LocatedDocument } from "./search-client.js";
export { IndexingClient } from "./indexing-client.js";
export type { IndexedDirectory, IndexedDirectories, IndexingMode, IndexingModes, IndexStatistics, IndexDocument, DocumentBatchResult, FileIndexResult, FileDeleteResult } from "./indexing-client.js";
export { ZoneClient } from "./zones.js";
export type {
  ZoneCreateInput,
  ZoneDelegationInput,
  ZoneDelegation,
  ZoneDelegationScopeRule,
  ZoneGrantInput,
  ZoneMount,
  ZoneOperation,
  ZoneRef,
} from "./zones.js";

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
  createHttpError,
  ServerError,
  NetworkError,
  TimeoutError,
  AbortError,
} from "./errors.js";

// Types
export type {
  NexusClientOptions,
  NativeIdentity,
  RequestOptions,
  ApiErrorResponse,
  PaginatedResponse,
  SseEvent,
} from "./types.js";

// Case transform utilities
export {
  snakeToCamel,
  camelToSnake,
  transformKeys,
  snakeToCamelKeys,
  camelToSnakeKeys,
} from "./case-transform.js";
