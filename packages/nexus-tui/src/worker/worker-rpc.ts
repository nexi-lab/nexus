/**
 * API worker thread entry point.
 *
 * Runs in a dedicated Bun Worker. Owns the real FetchClient and processes
 * HTTP requests sent from the main thread via postMessage, keeping blocking
 * I/O off the rendering event loop.
 *
 * Message flow: main sends ToWorkerMessage → worker sends FromWorkerMessage.
 *
 * @see §2 Worker thread isolation — Issue #3632
 */

import { FetchClient } from "@nexus-ai-fs/api-client";
import type {
  ToWorkerMessage,
  FromWorkerMessage,
  SerializableRequestOptions,
} from "./protocol.js";

// ─── Constants ────────────────────────────────────────────────────────────────

/** Warn in dev when a response payload exceeds this threshold. */
const LARGE_RESPONSE_WARN_BYTES = 512 * 1024; // 512 KB

// ─── Worker state ─────────────────────────────────────────────────────────────

let client: FetchClient | null = null;

/** AbortControllers for in-flight requests, keyed by request ID. */
const inFlight = new Map<string, AbortController>();

// ─── Helpers ──────────────────────────────────────────────────────────────────

function post(msg: FromWorkerMessage): void {
  self.postMessage(msg);
}

function buildOpts(
  options: SerializableRequestOptions | undefined,
  signal: AbortSignal,
): SerializableRequestOptions & { signal: AbortSignal } {
  return { ...options, signal };
}

// ─── Message handler ──────────────────────────────────────────────────────────

self.onmessage = async (event: MessageEvent<ToWorkerMessage>): Promise<void> => {
  const msg = event.data;

  switch (msg.type) {
    case "init": {
      client = new FetchClient(msg.config);
      post({ type: "ready" });
      break;
    }

    case "reconfigure": {
      // Replace FetchClient with new config (identity switch or reconnect).
      // In-flight requests on the old client will complete normally.
      client = new FetchClient(msg.config);
      break;
    }

    case "cancel": {
      inFlight.get(msg.id)?.abort();
      inFlight.delete(msg.id);
      break;
    }

    case "request": {
      if (!client) {
        post({ type: "error", id: msg.id, message: "Worker not initialized" });
        return;
      }

      const controller = new AbortController();
      inFlight.set(msg.id, controller);
      const opts = buildOpts(msg.options, controller.signal);

      try {
        if (msg.kind === "raw") {
          const response = await client.rawRequest(
            msg.method,
            msg.path,
            msg.body as string | undefined,
            opts,
          );
          const body = await response.text();
          const headers: [string, string][] = [];
          response.headers.forEach((value, key) => headers.push([key, value]));
          post({
            type: "raw-response",
            id: msg.id,
            result: {
              status: response.status,
              statusText: response.statusText,
              headers,
              body,
              ok: response.ok,
            },
          });
          return;
        }

        if (msg.kind === "void") {
          if (msg.method === "POST") {
            await client.postNoContent(msg.path, msg.body, opts);
          } else {
            // DELETE void
            await client.deleteNoContent(msg.path, opts);
          }
          post({ type: "response", id: msg.id, result: null });
          return;
        }

        // kind === 'json'
        let result: unknown;
        switch (msg.method) {
          case "GET":
            result = await client.get(msg.path, opts);
            break;
          case "POST":
            result = await client.post(msg.path, msg.body, opts);
            break;
          case "PUT":
            result = await client.put(msg.path, msg.body, opts);
            break;
          case "PATCH":
            result = await client.patch(msg.path, msg.body, opts);
            break;
          case "DELETE":
            result = await client.delete(msg.path, opts);
            break;
          default:
            post({
              type: "error",
              id: msg.id,
              message: `Unsupported HTTP method: ${msg.method as string}`,
            });
            return;
        }

        // Issue 13A: dev-mode warning for large payloads
        if (process.env.NODE_ENV !== "production" && result !== null && result !== undefined) {
          try {
            const approxBytes = JSON.stringify(result).length;
            if (approxBytes > LARGE_RESPONSE_WARN_BYTES) {
              console.warn(
                `[nexus-tui worker] Large response from ${msg.path}: ` +
                `~${Math.round(approxBytes / 1024)}KB. ` +
                "Consider adding pagination or tighter query filters.",
              );
            }
          } catch {
            // JSON.stringify failed (circular refs etc.) — skip the warning
          }
        }

        post({ type: "response", id: msg.id, result });
      } catch (err) {
        const message = err instanceof Error ? err.message : "Request failed";
        post({ type: "error", id: msg.id, message });
      } finally {
        inFlight.delete(msg.id);
      }
      break;
    }
  }
};
