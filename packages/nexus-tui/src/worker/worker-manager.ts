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
/** Healthy-window duration before restart counter resets. */
const RESTART_RESET_WINDOW_MS = 30_000;

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
  let restartScheduled = false;
  let restartTimer: ReturnType<typeof setTimeout> | null = null;
  let restartResetTimer: ReturnType<typeof setTimeout> | null = null;

  function clearRestartResetTimer(): void {
    if (restartResetTimer !== null) {
      clearTimeout(restartResetTimer);
      restartResetTimer = null;
    }
  }

  function scheduleRestartReset(readyPromise: Promise<void>): void {
    void readyPromise.then(() => {
      if (terminated) return;
      clearRestartResetTimer();
      restartResetTimer = setTimeout(() => {
        restartCount = 0;
        restartResetTimer = null;
      }, RESTART_RESET_WINDOW_MS);
    }).catch(() => {
      // Worker failed before ready; no reset scheduling needed.
    });
  }

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
      handleCrash(worker);
    };

    // Bun workers emit 'close' event (not 'exit') on termination
    worker.addEventListener("close", () => {
      if (terminated) return;
      handleCrash(worker);
    });

    worker.postMessage({ type: "init", config: currentConfig } satisfies ToWorkerMessage);

    return { worker, readyPromise };
  }

  // ─── Crash recovery ─────────────────────────────────────────────────────────

  function handleCrash(sourceWorker: Worker): void {
    if (terminated) return;
    if (sourceWorker !== currentWorker) return;
    if (restartScheduled) return;
    clearRestartResetTimer();

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

    restartScheduled = true;

    const restart = () => {
      restartScheduled = false;
      restartTimer = null;
      if (terminated) return;
      const { worker: newWorker, readyPromise: newReady } = spawn();
      currentWorker = newWorker;
      client._rewire(newWorker, newReady);
      scheduleRestartReset(newReady);
    };

    if (delay <= 0) {
      queueMicrotask(restart);
    } else {
      restartTimer = setTimeout(restart, delay);
    }
  }

  // ─── Bootstrap ──────────────────────────────────────────────────────────────

  const { worker: initialWorker, readyPromise: initialReady } = spawn();
  currentWorker = initialWorker;
  const client = new WorkerFetchClient(initialWorker, initialReady);
  scheduleRestartReset(initialReady);

  // ─── Public API ─────────────────────────────────────────────────────────────

  return {
    client,

    reconfigure(config: NexusClientOptions): void {
      currentConfig = config;
      currentWorker?.postMessage({ type: "reconfigure", config } satisfies ToWorkerMessage);
    },

    terminate(): void {
      terminated = true;
      if (restartTimer !== null) {
        clearTimeout(restartTimer);
        restartTimer = null;
      }
      clearRestartResetTimer();
      restartScheduled = false;
      currentWorker?.terminate();
      currentWorker = null;
    },
  };
}
