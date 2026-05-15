/**
 * Typed message protocol for the API worker thread.
 *
 * Messages flow in two directions:
 *   Main → Worker: ToWorkerMessage
 *   Worker → Main: FromWorkerMessage
 *
 * AbortSignal is NOT transferable across thread boundaries. Cancellation is
 * handled by the main thread sending a 'cancel' message with the request ID.
 *
 * @see §2 Worker thread isolation — Issue #3632
 */

import type { NexusClientOptions } from "@nexus-ai-fs/api-client";

// ─── Shared types ─────────────────────────────────────────────────────────────

/** Request options that are safe to send across the message channel. */
export interface SerializableRequestOptions {
  readonly timeout?: number;
  readonly idempotencyKey?: string;
  readonly headers?: Readonly<Record<string, string>>;
}

/**
 * Serialized HTTP Response for rawRequest calls.
 * Response objects are not structured-cloneable, so we serialize them manually.
 */
export interface SerializedResponse {
  readonly status: number;
  readonly statusText: string;
  readonly headers: readonly [string, string][];
  readonly body: string;
  readonly ok: boolean;
}

/**
 * How the worker should handle the HTTP response.
 *   'json'  — parse as JSON and return typed result
 *   'void'  — no response body expected (204)
 *   'raw'   — return serialized Response (for rawRequest callers)
 */
export type RequestKind = "json" | "void" | "raw";

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE" | "HEAD";

// ─── Main → Worker ────────────────────────────────────────────────────────────

export type ToWorkerMessage =
  | {
      /** Initialize the worker with connection config. Worker replies with 'ready'. */
      readonly type: "init";
      readonly config: NexusClientOptions;
    }
  | {
      /** Update the worker's FetchClient config (identity switch, reconnect). */
      readonly type: "reconfigure";
      readonly config: NexusClientOptions;
    }
  | {
      /** Execute an HTTP request. Worker replies with 'response', 'raw-response', or 'error'. */
      readonly type: "request";
      readonly id: string;
      readonly method: HttpMethod;
      readonly path: string;
      readonly body?: unknown;
      readonly options?: SerializableRequestOptions;
      readonly kind: RequestKind;
    }
  | {
      /** Cancel an in-flight request by ID. Worker aborts its internal AbortController. */
      readonly type: "cancel";
      readonly id: string;
    };

// ─── Worker → Main ────────────────────────────────────────────────────────────

export type FromWorkerMessage =
  | {
      /** Worker finished init and is ready to process requests. */
      readonly type: "ready";
    }
  | {
      /** Successful JSON response. */
      readonly type: "response";
      readonly id: string;
      readonly result: unknown;
    }
  | {
      /** Successful rawRequest response — serialized Response object. */
      readonly type: "raw-response";
      readonly id: string;
      readonly result: SerializedResponse;
    }
  | {
      /** Request failed. The message string is compatible with categorizeError(). */
      readonly type: "error";
      readonly id: string;
      readonly message: string;
    };
