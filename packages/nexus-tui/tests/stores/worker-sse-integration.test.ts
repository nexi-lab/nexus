/**
 * Integration test: SSE bus + WorkerFetchClient reconnect interaction.
 *
 * Verifies that after a simulated disconnect/reconnect cycle:
 *   1. SSE handlers remain correctly registered (no duplicates)
 *   2. The WorkerFetchClient recovers and processes new requests
 *   3. SSE events dispatched after reconnect reach registered handlers
 *
 * Uses in-process mocks — no real network, no real worker thread.
 *
 * @see Issue #3632 §2 + §3 — Worker isolation + SSE streaming
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { useSseBus, _testInternals } from "../../src/stores/sse-bus.js";
import { WorkerFetchClient } from "../../src/worker/worker-client.js";
import type { FromWorkerMessage, ToWorkerMessage } from "../../src/worker/protocol.js";

const { handlers, dispatch } = _testInternals;

// ─── MockWorker (same pattern as worker-client.test.ts) ───────────────────────

class MockWorker {
  onmessage: ((event: MessageEvent<FromWorkerMessage>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  readonly sent: ToWorkerMessage[] = [];
  private readonly _listeners = new Map<string, Set<EventListenerOrEventListenerObject>>();

  postMessage(msg: ToWorkerMessage): void {
    this.sent.push(msg);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    if (!this._listeners.has(type)) this._listeners.set(type, new Set());
    this._listeners.get(type)!.add(listener);
  }

  removeEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    this._listeners.get(type)?.delete(listener);
  }

  reply(msg: FromWorkerMessage): void {
    const event = { data: msg } as MessageEvent<FromWorkerMessage>;
    this.onmessage?.(event);
    for (const listener of this._listeners.get("message") ?? []) {
      if (typeof listener === "function") (listener as EventListener)(event as unknown as Event);
    }
  }

  terminate(): void { /* no-op */ }
}

function createClient(worker: MockWorker): WorkerFetchClient {
  return new WorkerFetchClient(worker as unknown as Worker, Promise.resolve());
}

function tick(): Promise<void> {
  return new Promise((resolve) => queueMicrotask(resolve));
}

const HANDLER_ID = "test:integration-handler";

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("Worker + SSE reconnect integration", () => {
  let worker: MockWorker;
  let client: WorkerFetchClient;
  let received: unknown[];

  beforeEach(() => {
    received = [];
    worker = new MockWorker();
    client = createClient(worker);

    // Register SSE handler — simulates what a domain store does at startup
    useSseBus.getState().registerHandler(
      HANDLER_ID,
      (events) => { received.push(...events); },
      { debounceMs: 0 },
    );
  });

  afterEach(() => {
    useSseBus.getState().unregisterHandler(HANDLER_ID);
    useSseBus.getState().disconnect();
    _testInternals.clearAllTimers();
  });

  it("SSE handler receives dispatched events", () => {
    dispatch({ raw: { event: "event", data: "{}" }, type: "write", path: "/a.txt", payload: {} });
    expect(received).toHaveLength(1);
  });

  it("after worker _rejectAll + _rewire, new requests succeed", async () => {
    // Simulate worker crash: reject all in-flight, rewire to new worker
    const inFlightPromise = client.get<string>("/api/v2/items", { timeout: 30_000 });
    await tick();

    client._rejectAll(new Error("Worker crashed"));
    await expect(inFlightPromise).rejects.toThrow("Worker crashed");

    const newWorker = new MockWorker();
    client._rewire(newWorker as unknown as Worker, Promise.resolve());

    const recoveredPromise = client.get<string>("/api/v2/items");
    await tick();

    const req = newWorker.sent.find((m) => m.type === "request");
    expect(req).toBeDefined();
    if (req?.type !== "request") return;

    newWorker.reply({ type: "response", id: req.id, result: "recovered" });
    expect(await recoveredPromise).toBe("recovered");
  });

  it("SSE handler is still active after worker restart — no duplicate registration", async () => {
    // Verify handler is registered
    expect(handlers.has(HANDLER_ID)).toBe(true);

    // Simulate worker crash + rewire
    client._rejectAll(new Error("crash"));
    const newWorker = new MockWorker();
    client._rewire(newWorker as unknown as Worker, Promise.resolve());

    // SSE handler count should be unchanged (still exactly 1 for this ID)
    expect(handlers.has(HANDLER_ID)).toBe(true);

    // Dispatch a new SSE event — should still arrive
    dispatch({ raw: { event: "event", data: "{}" }, type: "delete", path: "/b.txt", payload: {} });
    expect(received).toHaveLength(1);
    expect((received[0] as { type: string }).type).toBe("delete");
  });

  it("re-registering SSE handler with same ID after unregister is safe", () => {
    useSseBus.getState().unregisterHandler(HANDLER_ID);
    expect(() => {
      useSseBus.getState().registerHandler(
        HANDLER_ID,
        (events) => { received.push(...events); },
        { debounceMs: 0 },
      );
    }).not.toThrow();

    dispatch({ raw: { event: "event", data: "{}" }, type: "write", path: "/c.txt", payload: {} });
    expect(received).toHaveLength(1);
  });

  it("worker requests and SSE events are independent — worker crash does not affect SSE", async () => {
    // Start an in-flight request
    const requestPromise = client.get("/api/v2/agents", { timeout: 30_000 });
    await tick();

    // Worker crashes — request fails
    client._rejectAll(new Error("crash"));
    await expect(requestPromise).rejects.toThrow("crash");

    // SSE still works after the crash
    dispatch({ raw: { event: "event", data: "{}" }, type: "agent_ready", payload: {} });
    expect(received).toHaveLength(1);
  });
});
