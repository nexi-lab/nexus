/**
 * Unit tests for WorkerManager — crash detection, auto-restart, and reconfigure.
 *
 * Uses a MockWorkerConstructor that returns MockWorker instances so no real
 * worker thread is spawned. Tests focus on lifecycle behavior:
 *   - Happy-path spawn + request round-trip (via WorkerFetchClient)
 *   - Crash mid-flight: in-flight requests rejected, replacement spawned (Issue 3A)
 *   - Replacement worker receives correct config via 'init' message (Issue 6A)
 *   - Reconfigure propagated to current worker (Issue 6A)
 *   - Double-crash (crash while restarting) handled without stack overflow
 *   - MAX_RESTARTS: after 5 crashes, no further restart attempt
 *   - terminate() stops the worker and prevents further restarts
 *
 * @see §2 Worker thread isolation — Issue #3632
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "bun:test";
import type { ToWorkerMessage, FromWorkerMessage } from "../../src/worker/protocol.js";

// ─── MockWorker ───────────────────────────────────────────────────────────────

class MockWorker {
  onmessage: ((event: MessageEvent<FromWorkerMessage>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  readonly sent: ToWorkerMessage[] = [];
  private readonly _listeners = new Map<string, Set<EventListenerOrEventListenerObject>>();
  terminated = false;

  postMessage(msg: ToWorkerMessage): void {
    this.sent.push(msg);
    // Auto-reply 'ready' to any 'init' message
    if (msg.type === "init") {
      queueMicrotask(() => this.dispatchMessage({ type: "ready" }));
    }
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    if (!this._listeners.has(type)) this._listeners.set(type, new Set());
    this._listeners.get(type)!.add(listener);
  }

  removeEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    this._listeners.get(type)?.delete(listener);
  }

  dispatchMessage(data: FromWorkerMessage): void {
    const event = { data } as MessageEvent<FromWorkerMessage>;
    this.onmessage?.(event);
    for (const listener of this._listeners.get("message") ?? []) {
      if (typeof listener === "function") (listener as EventListener)(event as unknown as Event);
    }
  }

  simulateCrash(message = "Worker script error"): void {
    const event = { message } as ErrorEvent;
    this.onerror?.(event);
  }

  simulateClose(): void {
    for (const listener of this._listeners.get("close") ?? []) {
      if (typeof listener === "function") (listener as EventListener)(new Event("close"));
    }
  }

  reply(msg: FromWorkerMessage): void {
    this.dispatchMessage(msg);
  }

  terminate(): void {
    this.terminated = true;
  }
}

// ─── MockWorkerManager (inline version for testing) ──────────────────────────

// We can't import createWorkerManager directly because it uses `new Worker(URL)`.
// Instead we test the behavior via a test-double that mirrors the real manager,
// or we extract and test the behavior through the public interface.
//
// Strategy: Patch the Worker constructor used by worker-manager.ts by
// monkey-patching globalThis.Worker before the import.

let workers: MockWorker[] = [];
let originalWorker: typeof Worker;

function tick(): Promise<void> {
  return new Promise((resolve) => queueMicrotask(resolve));
}

async function tickN(n: number): Promise<void> {
  for (let i = 0; i < n; i++) await tick();
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("WorkerManager", () => {
  let createWorkerManager: typeof import("../../src/worker/worker-manager.js").createWorkerManager;

  beforeEach(async () => {
    workers = [];
    originalWorker = globalThis.Worker;

    // Replace global Worker with factory that creates MockWorkers
    (globalThis as unknown as Record<string, unknown>).Worker = class {
      constructor(_url: string | URL, _opts?: WorkerOptions) {
        const w = new MockWorker();
        workers.push(w);
        return w;
      }
    } as unknown as typeof Worker;

    // Dynamic import to pick up the mocked Worker
    const mod = await import("../../src/worker/worker-manager.js?t=" + Date.now());
    createWorkerManager = mod.createWorkerManager;
  });

  afterEach(() => {
    (globalThis as unknown as Record<string, unknown>).Worker = originalWorker;
  });

  const baseConfig = { apiKey: "nx_test_key", baseUrl: "http://localhost:2026" };

  // ─── Happy path ──────────────────────────────────────────────────────────

  it("spawns one worker on creation and sends init message", async () => {
    const manager = createWorkerManager(baseConfig);
    await tickN(3); // allow ready to fire

    expect(workers).toHaveLength(1);
    const initMsg = workers[0].sent.find((m) => m.type === "init");
    expect(initMsg).toBeDefined();
    if (initMsg?.type !== "init") return;
    expect(initMsg.config.apiKey).toBe(baseConfig.apiKey);

    manager.terminate();
  });

  it("client resolves requests after worker is ready", async () => {
    const manager = createWorkerManager(baseConfig);
    await tickN(3);

    const promise = manager.client.get<string>("/api/v2/test");
    await tick();

    const req = workers[0].sent.find((m) => m.type === "request");
    expect(req).toBeDefined();
    if (req?.type !== "request") return;

    workers[0].reply({ type: "response", id: req.id, result: "ok" });
    expect(await promise).toBe("ok");

    manager.terminate();
  });

  // ─── reconfigure (Issue 6A) ──────────────────────────────────────────────

  it("reconfigure sends reconfigure message to current worker", async () => {
    const manager = createWorkerManager(baseConfig);
    await tickN(3);

    const newConfig = { ...baseConfig, agentId: "bot-1" };
    manager.reconfigure(newConfig);

    const msg = workers[0].sent.find((m) => m.type === "reconfigure");
    expect(msg).toBeDefined();
    if (msg?.type !== "reconfigure") return;
    expect(msg.config.agentId).toBe("bot-1");

    manager.terminate();
  });

  // ─── Crash recovery (Issue 3A) ───────────────────────────────────────────

  it("crash mid-flight: in-flight requests are rejected", async () => {
    const manager = createWorkerManager(baseConfig);
    await tickN(3);

    const promise = manager.client.get("/api/v2/slow", { timeout: 30_000 });
    await tick();

    workers[0].simulateCrash();

    await expect(promise).rejects.toThrow(/crashed/i);
  });

  it("after crash, a new worker is spawned and requests succeed", async () => {
    const manager = createWorkerManager(baseConfig);
    await tickN(3);

    // Crash the first worker
    workers[0].simulateCrash();
    // Wait for backoff (0ms for first restart) + spawn + ready
    await tickN(5);

    expect(workers).toHaveLength(2);

    const promise = manager.client.get<string>("/api/v2/test");
    await tick();

    const req = workers[1].sent.find((m) => m.type === "request");
    expect(req).toBeDefined();
    if (req?.type !== "request") return;

    workers[1].reply({ type: "response", id: req.id, result: "recovered" });
    expect(await promise).toBe("recovered");

    manager.terminate();
  });

  it("does not restart twice for duplicate crash signals from one worker", async () => {
    const manager = createWorkerManager(baseConfig);
    await tickN(3);

    // Same crash can emit both onerror and close in real runtimes.
    workers[0].simulateCrash();
    workers[0].simulateClose();
    await tickN(5);

    expect(workers).toHaveLength(2);

    // A late close event from the retired worker should be ignored.
    workers[0].simulateClose();
    await tickN(5);
    expect(workers).toHaveLength(2);

    manager.terminate();
  });

  it("replacement worker receives correct config via init", async () => {
    const manager = createWorkerManager(baseConfig);
    await tickN(3);

    // Reconfigure before crash so we verify the updated config propagates
    manager.reconfigure({ ...baseConfig, agentId: "agent-xyz" });
    workers[0].simulateCrash();
    await tickN(5);

    const initMsg = workers[1].sent.find((m) => m.type === "init");
    expect(initMsg).toBeDefined();
    if (initMsg?.type !== "init") return;
    expect(initMsg.config.agentId).toBe("agent-xyz");

    manager.terminate();
  });

  // ─── Restart limit ───────────────────────────────────────────────────────

  it("stops restarting after MAX_RESTARTS crashes", async () => {
    vi.useFakeTimers();
    try {
      const manager = createWorkerManager(baseConfig);
      await tickN(3);

      const MAX_RESTARTS = 5;
      for (let i = 0; i < MAX_RESTARTS; i++) {
        workers[workers.length - 1].simulateCrash();
        vi.advanceTimersByTime(5_000);
        await tickN(5);
      }

      const countBefore = workers.length;
      // One more crash — should NOT spawn another worker
      workers[workers.length - 1].simulateCrash();
      vi.advanceTimersByTime(5_000);
      await tickN(5);
      expect(workers.length).toBe(countBefore);

      manager.terminate();
    } finally {
      vi.useRealTimers();
    }
  });

  it("resets restart counter after worker is healthy for 30s", async () => {
    vi.useFakeTimers();
    try {
      const manager = createWorkerManager(baseConfig);
      await tickN(3);

      // Burn one restart so the counter is non-zero.
      workers[workers.length - 1].simulateCrash();
      await tickN(5);
      expect(workers).toHaveLength(2);

      // Keep the replacement worker healthy long enough to reset restartCount.
      vi.advanceTimersByTime(30_000);
      await tickN(3);

      // After reset, we should still be allowed 5 more restart attempts.
      const baseCount = workers.length;
      for (let i = 0; i < 5; i++) {
        workers[workers.length - 1].simulateCrash();
        vi.advanceTimersByTime(5_000);
        await tickN(5);
      }
      expect(workers.length).toBe(baseCount + 5);

      // The next crash should hit MAX_RESTARTS and stop spawning.
      const countBeforeFinalCrash = workers.length;
      workers[workers.length - 1].simulateCrash();
      vi.advanceTimersByTime(5_000);
      await tickN(5);
      expect(workers.length).toBe(countBeforeFinalCrash);

      manager.terminate();
    } finally {
      vi.useRealTimers();
    }
  });

  // ─── terminate ───────────────────────────────────────────────────────────

  it("terminate() stops the current worker and prevents restart on crash", async () => {
    const manager = createWorkerManager(baseConfig);
    await tickN(3);

    manager.terminate();
    expect(workers[0].terminated).toBe(true);

    const countBefore = workers.length;
    workers[0].simulateCrash();
    await tickN(5);
    // No new worker spawned
    expect(workers.length).toBe(countBefore);
  });
});
