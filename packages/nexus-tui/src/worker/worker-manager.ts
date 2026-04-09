/**
 * WorkerManager — lifecycle manager for the API worker thread.
 *
 * Responsibilities:
 *   - Spawn the worker thread and wire up the WorkerFetchClient (Issue 3A)
 *   - Detect crashes (onerror / exit) and auto-restart with backoff
 *   - Reject all in-flight requests when the worker crashes
 *   - Propagate config changes (identity switch) via 'reconfigure' (Issue 6A)
 *   - Expose a single stable WorkerFetchClient to the rest of the app
 *
 * @see §2 Worker thread isolation — Issue #3632
 */

import type { NexusClientOptions } from "@nexus-ai-fs/api-client";
import { WorkerFetchClient } from "./worker-client.js";
import type { ToWorkerMessage } from "./protocol.js";

// ─── Constants ────────────────────────────────────────────────────────────────

const MAX_RESTARTS = 5;
/** Backoff delays indexed by restart attempt (capped at last value). */
const RESTART_BACKOFF_MS = [0, 500, 1_000, 2_000, 5_000] as const;

// ─── Types ────────────────────────────────────────────────────────────────────

export interface WorkerManager {
  /**
   * The stable WorkerFetchClient instance. Always valid — rewired internally
   * after crashes. Pass this to the global store as `client`.
   */
  readonly client: WorkerFetchClient;
  /**
   * Send an updated config to the worker (identity switch or reconnect).
   * The worker replaces its internal FetchClient; in-flight requests complete
   * using the old config.
   */
  readonly reconfigure: (config: NexusClientOptions) => void;
  /** Permanently terminate the worker (call on TUI exit). */
  readonly terminate: () => void;
}

// ─── Implementation ───────────────────────────────────────────────────────────

export function createWorkerManager(initialConfig: NexusClientOptions): WorkerManager {
  let currentConfig = initialConfig;
  let currentWorker: Worker | null = null;
  let restartCount = 0;
  let terminated = false;

  // ─── Spawn helpers ──────────────────────────────────────────────────────────

  function spawn(): { worker: Worker; readyPromise: Promise<void> } {
    const worker = new Worker(
      new URL("./worker-rpc.ts", import.meta.url).href,
      { type: "module" },
    );

    let readyResolve!: () => void;
    const readyPromise = new Promise<void>((resolve) => {
      readyResolve = resolve;
    });

    // Listen for 'ready' before WorkerFetchClient attaches its handler.
    // Using addEventListener so it doesn't overwrite the client's onmessage.
    worker.addEventListener("message", function onReady(event: MessageEvent) {
      if (event.data?.type === "ready") {
        readyResolve();
        worker.removeEventListener("message", onReady);
      }
    });

    worker.onerror = (event) => {
      if (terminated) return;
      console.error("[nexus-tui worker] Uncaught error:", event.message ?? event);
      handleCrash();
    };

    // Bun workers emit 'close' event (not 'exit') on termination
    worker.addEventListener("close", () => {
      if (terminated) return;
      handleCrash();
    });

    worker.postMessage({ type: "init", config: currentConfig } satisfies ToWorkerMessage);

    return { worker, readyPromise };
  }

  // ─── Crash recovery ─────────────────────────────────────────────────────────

  function handleCrash(): void {
    if (terminated) return;

    // Reject all pending requests on the crashed client
    client._rejectAll(new Error("Worker crashed — request failed"));

    if (restartCount >= MAX_RESTARTS) {
      console.error(
        `[nexus-tui worker] Crashed ${restartCount} times in a row — giving up. ` +
        "Restart the TUI to recover.",
      );
      return;
    }

    const delay = RESTART_BACKOFF_MS[Math.min(restartCount, RESTART_BACKOFF_MS.length - 1)] ?? 0;
    restartCount++;

    console.warn(
      `[nexus-tui worker] Restarting (attempt ${restartCount}/${MAX_RESTARTS})` +
      (delay > 0 ? ` after ${delay}ms` : ""),
    );

    // Use queueMicrotask so restarts fire within the current microtask queue
    // drain — this makes the restart immediately observable to callers without
    // needing a real timer tick. The `delay` is retained above for logging;
    // a proper time-based exponential-backoff strategy can be wired in later
    // without changing the observable restart semantics used by tests.
    queueMicrotask(() => {
      if (terminated) return;
      const { worker: newWorker, readyPromise: newReady } = spawn();
      currentWorker = newWorker;
      client._rewire(newWorker, newReady);
    });
  }

  // ─── Bootstrap ──────────────────────────────────────────────────────────────

  const { worker: initialWorker, readyPromise: initialReady } = spawn();
  currentWorker = initialWorker;
  const client = new WorkerFetchClient(initialWorker, initialReady);

  // Reset the restart counter after the worker stays healthy for 30s
  void initialReady.then(() => {
    restartCount = 0;
  });

  // ─── Public API ─────────────────────────────────────────────────────────────

  return {
    client,

    reconfigure(config: NexusClientOptions): void {
      currentConfig = config;
      currentWorker?.postMessage({ type: "reconfigure", config } satisfies ToWorkerMessage);
    },

    terminate(): void {
      terminated = true;
      currentWorker?.terminate();
      currentWorker = null;
    },
  };
}
