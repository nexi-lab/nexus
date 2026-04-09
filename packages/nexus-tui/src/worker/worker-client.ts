/**
 * WorkerFetchClient — main-thread proxy for the API worker.
 *
 * Implements the same public API as FetchClient (get, post, put, patch, delete,
 * postNoContent, deleteNoContent, rawRequest) but routes every call through the
 * worker thread via typed JSON-RPC messages.
 *
 * Features:
 *   - Correlation ID per request (Issue 4A)
 *   - readyPromise gate — awaited before sending any request (Issue 15A)
 *   - GET request deduplication (Issue 14A)
 *   - Per-request timeout with AbortController (Issue 7A)
 *   - AbortSignal cancellation propagated to worker (Issue 16A)
 *   - Large-payload dev warning on deserialization (Issue 13A)
 *
 * @see §2 Worker thread isolation — Issue #3632
 */

import type { RequestOptions } from "@nexus-ai-fs/api-client";
import type {
  FromWorkerMessage,
  HttpMethod,
  RequestKind,
  SerializedResponse,
  SerializableRequestOptions,
  ToWorkerMessage,
} from "./protocol.js";

// ─── Constants ────────────────────────────────────────────────────────────────

const DEFAULT_TIMEOUT_MS = 30_000;

/** Warn in dev when structured-clone deserialization exceeds this. */
const DESER_WARN_MS = 10;

// ─── Helpers ──────────────────────────────────────────────────────────────────

let idCounter = 0;
function nextId(): string {
  return `rpc-${(++idCounter).toString(36)}-${Date.now().toString(36)}`;
}

/** Strip AbortSignal (not transferable) and extract serializable options. */
function serializeOptions(
  options: RequestOptions | undefined,
): SerializableRequestOptions | undefined {
  if (!options) return undefined;
  const out: Record<string, unknown> = {};
  if (options.timeout !== undefined) out.timeout = options.timeout;
  if (options.idempotencyKey !== undefined) out.idempotencyKey = options.idempotencyKey;
  if (options.headers !== undefined) out.headers = options.headers;
  return Object.keys(out).length > 0 ? (out as SerializableRequestOptions) : undefined;
}

// ─── WorkerFetchClient ────────────────────────────────────────────────────────

interface Pending {
  readonly resolve: (value: unknown) => void;
  readonly reject: (reason: Error) => void;
}

export class WorkerFetchClient {
  private worker: Worker;
  private readonly pending = new Map<string, Pending>();
  /** In-flight GET requests keyed by path — deduplicated (Issue 14A). */
  private readonly dedup = new Map<string, Promise<unknown>>();
  /**
   * Resolves when the worker has acknowledged 'init'.
   * Replaced by WorkerManager after a crash restart.
   */
  private readyInternal: Promise<void>;

  /** Public accessor for the ready promise (used by WorkerManager). */
  get ready(): Promise<void> { return this.readyInternal; }

  constructor(worker: Worker, readyPromise: Promise<void>) {
    this.worker = worker;
    this.readyInternal = readyPromise;
    this.attachMessageHandler(worker);
  }

  private attachMessageHandler(worker: Worker): void {
    worker.onmessage = (event: MessageEvent<FromWorkerMessage>) => {
      const msg = event.data;
      if (msg.type === "ready") return; // handled by WorkerManager

      const pending = this.pending.get(msg.id);
      if (!pending) return; // already resolved, cancelled, or timed out
      this.pending.delete(msg.id);

      if (msg.type === "response" || msg.type === "raw-response") {
        pending.resolve(msg.result);
      } else {
        pending.reject(new Error(msg.message));
      }
    };
  }

  /**
   * Rewire this client to a new worker after a crash/restart.
   * Called exclusively by WorkerManager — not part of the public API.
   * @internal
   */
  _rewire(newWorker: Worker, newReady: Promise<void>): void {
    this.worker = newWorker;
    this.readyInternal = newReady;
    this.attachMessageHandler(newWorker);
  }

  /**
   * Reject all pending requests with the given error.
   * Called by WorkerManager when the worker crashes.
   * @internal
   */
  _rejectAll(error: Error): void {
    for (const pending of this.pending.values()) {
      pending.reject(error);
    }
    this.pending.clear();
    this.dedup.clear();
  }

  // ─── Core send ─────────────────────────────────────────────────────────────

  private send<T>(
    method: HttpMethod,
    path: string,
    body: unknown,
    options: RequestOptions | undefined,
    kind: RequestKind,
  ): Promise<T> {
    // Issue 14A: deduplicate concurrent identical GET requests.
    // Checked before the readyInternal gate so concurrent same-path GETs
    // share one in-flight promise immediately.
    if (kind === "json" && method === "GET") {
      const existing = this.dedup.get(path);
      if (existing) return existing as Promise<T>;
    }

    const id = nextId();
    const serializableOpts = serializeOptions(options);
    const timeoutMs = options?.timeout ?? DEFAULT_TIMEOUT_MS;
    const signal = options?.signal;

    // Issue 16A: already-aborted signal — reject immediately without any setup.
    if (signal?.aborted) {
      const p = Promise.reject<T>(new Error("Request aborted"));
      void p.catch(() => {}); // pre-handle so Bun never sees it as unhandled
      return p;
    }

    // `send()` is intentionally NOT async. An async wrapper would create a
    // second Promise between the inner promise and the caller, and Bun's
    // synchronous unhandled-rejection detector would fire on that wrapper in
    // the microtask between when `reject()` is called and when the caller's
    // `await` suspends. By creating one concrete Promise here and attaching
    // `void promise.catch(() => {})` before returning, the promise is always
    // "handled" from the moment it exists.

    let timeoutId: ReturnType<typeof setTimeout> | null = null;

    const cleanup = (fn: () => void) => {
      if (timeoutId !== null) clearTimeout(timeoutId);
      fn();
    };

    const promise = new Promise<T>((resolve, reject) => {
      // Per-request timeout (Issue 7A)
      timeoutId = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          this.sendCancel(id);
          reject(new Error(`timeout: ${method} ${path} exceeded ${timeoutMs}ms`));
        }
      }, timeoutMs);

      this.pending.set(id, {
        resolve: (v) => cleanup(() => resolve(v as T)),
        reject: (e) => cleanup(() => reject(e)),
      });

      // Issue 16A: propagate AbortSignal cancellation into the worker
      if (signal) {
        signal.addEventListener(
          "abort",
          () => {
            if (this.pending.has(id)) {
              this.pending.delete(id);
              this.sendCancel(id);
              cleanup(() => reject(new Error("Request aborted")));
            }
          },
          { once: true },
        );
      }
    });

    // Silence unhandled-rejection tracking. Since `send()` is not async,
    // the promise the caller holds IS this promise — the noop .catch() means
    // Bun will never see it as unhandled regardless of when reject() fires.
    void promise.catch(() => {});

    // Issue 15A: gate postMessage on the worker being ready, then send.
    // Uses .then() rather than await to avoid an async wrapper (see above).
    void this.readyInternal.then(() => {
      // Guard: pending entry may have been cleared by timeout/abort/rejectAll
      // before readyInternal resolved (e.g., during a slow worker start).
      if (this.pending.has(id)) {
        const msg: ToWorkerMessage = {
          type: "request",
          id,
          method,
          path,
          body,
          options: serializableOpts,
          kind,
        };
        this.worker.postMessage(msg);
      }
    });

    // Register in dedup map; remove on settle (Issue 14A).
    // `.finally()` propagates rejection to the derived promise, so we must
    // attach `.catch(() => {})` to that derived promise too.
    if (kind === "json" && method === "GET") {
      this.dedup.set(path, promise);
      void promise.finally(() => {
        if (this.dedup.get(path) === promise) this.dedup.delete(path);
      }).catch(() => {});
    }

    // Issue 13A: dev-mode deserialization timing (side-effect only, not in return chain).
    if (process.env.NODE_ENV !== "production") {
      void promise.then((result) => {
        const start = performance.now();
        try { structuredClone(result); } catch { /* not cloneable — skip */ }
        const elapsed = performance.now() - start;
        if (elapsed > DESER_WARN_MS) {
          console.warn(
            `[nexus-tui] Slow deserialization: ${method} ${path} took ${elapsed.toFixed(1)}ms. ` +
            "Consider adding server-side pagination.",
          );
        }
      }).catch(() => {});
    }

    return promise;
  }

  private sendCancel(id: string): void {
    // Best-effort — worker may already be dead if we're mid-crash
    try { this.worker.postMessage({ type: "cancel", id } satisfies ToWorkerMessage); } catch { /**/ }
  }

  // ─── FetchClient-compatible public API ────────────────────────────────────
  //
  // These methods are intentionally NOT async. `send()` already returns a
  // Promise; adding `async` here would wrap it in a second async Promise,
  // creating an unhandled-rejection window between when the inner promise
  // rejects and when the outer async-wrapper promise rejects. By returning
  // `send()`'s Promise directly and pre-attaching a noop `.catch()`, Bun's
  // synchronous unhandled-rejection detector never sees a bare rejection.

  get<T>(path: string, options?: RequestOptions): Promise<T> {
    const p = this.send<T>("GET", path, undefined, options, "json");
    void p.catch(() => {});
    return p;
  }

  post<T>(path: string, body: unknown, options?: RequestOptions): Promise<T> {
    const p = this.send<T>("POST", path, body, options, "json");
    void p.catch(() => {});
    return p;
  }

  put<T>(path: string, body: unknown, options?: RequestOptions): Promise<T> {
    const p = this.send<T>("PUT", path, body, options, "json");
    void p.catch(() => {});
    return p;
  }

  patch<T>(path: string, body: unknown, options?: RequestOptions): Promise<T> {
    const p = this.send<T>("PATCH", path, body, options, "json");
    void p.catch(() => {});
    return p;
  }

  delete<T>(path: string, options?: RequestOptions): Promise<T> {
    const p = this.send<T>("DELETE", path, undefined, options, "json");
    void p.catch(() => {});
    return p;
  }

  postNoContent(path: string, body?: unknown, options?: RequestOptions): Promise<void> {
    // `.then(() => undefined)` converts Promise<null> → Promise<void>; the
    // .then() also counts as a rejection handler on send()'s promise so Bun
    // doesn't flag it, then we suppress the chained promise too.
    const p = this.send<null>("POST", path, body, options, "void").then(() => undefined as void);
    void p.catch(() => {});
    return p;
  }

  deleteNoContent(path: string, options?: RequestOptions): Promise<void> {
    const p = this.send<null>("DELETE", path, undefined, options, "void").then(() => undefined as void);
    void p.catch(() => {});
    return p;
  }

  rawRequest(
    method: string,
    path: string,
    body?: string,
    options?: RequestOptions,
  ): Promise<Response> {
    const p = this.send<SerializedResponse>(
      method as HttpMethod,
      path,
      body,
      options,
      "raw",
    ).then((serialized) => new Response(serialized.body, {
      status: serialized.status,
      statusText: serialized.statusText,
      headers: new Headers(serialized.headers as [string, string][]),
    }));
    void p.catch(() => {});
    return p;
  }
}
