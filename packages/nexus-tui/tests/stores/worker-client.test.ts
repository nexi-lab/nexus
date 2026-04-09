/**
 * Unit tests for WorkerFetchClient — main-thread proxy for the API worker.
 *
 * Uses a MockWorker that responds synchronously in the same process, so no
 * real worker thread is spawned. Tests focus on protocol correctness:
 *   - Normal request/response round-trips
 *   - Correlation ID routing (concurrent requests don't cross-wire)
 *   - GET deduplication (Issue 14A)
 *   - Timeout fires correctly and sends cancel to worker (Issue 7A)
 *   - AbortSignal cancellation propagated (Issue 16A)
 *   - Late worker response after timeout is silently dropped
 *   - readyPromise gate — requests queue until worker is ready (Issue 15A)
 *   - rawRequest returns a reconstructed Response
 *   - void (postNoContent / deleteNoContent) resolves with no value
 *
 * @see §2 Worker thread isolation — Issue #3632
 */

import { describe, it, expect, beforeEach } from "bun:test";
import { WorkerFetchClient } from "../../src/worker/worker-client.js";
import type { ToWorkerMessage, FromWorkerMessage } from "../../src/worker/protocol.js";

// ─── MockWorker ───────────────────────────────────────────────────────────────

/**
 * In-process mock that implements the subset of the Worker API used by
 * WorkerFetchClient. Messages are accumulated and can be flushed manually.
 */
class MockWorker {
  onmessage: ((event: MessageEvent<FromWorkerMessage>) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;

  /** Messages sent from main thread via worker.postMessage(). */
  readonly sent: ToWorkerMessage[] = [];

  private _listeners: Map<string, Set<EventListenerOrEventListenerObject>> = new Map();

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

  /** Simulate the worker sending a message back to main. */
  reply(msg: FromWorkerMessage): void {
    const event = { data: msg } as MessageEvent<FromWorkerMessage>;
    this.onmessage?.(event);
  }

  /** Simulate the worker sending a 'ready' message (for manager-created ready promise). */
  dispatchReady(): void {
    const event = { data: { type: "ready" } } as MessageEvent;
    for (const listener of this._listeners.get("message") ?? []) {
      if (typeof listener === "function") listener(event);
      else (listener as EventListener).call(this, event);
    }
  }

  terminate(): void { /* no-op */ }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function createClient(worker: MockWorker, ready = true): WorkerFetchClient {
  const readyPromise = ready
    ? Promise.resolve()
    : new Promise<void>((resolve) => {
        // Listen for 'ready' via addEventListener (same as WorkerManager)
        worker.addEventListener("message", function onReady(event: Event) {
          const me = event as MessageEvent;
          if (me.data?.type === "ready") {
            resolve();
            worker.removeEventListener("message", onReady);
          }
        });
      });

  return new WorkerFetchClient(worker as unknown as Worker, readyPromise);
}

/** Flush pending microtasks (one tick). */
function tick(): Promise<void> {
  return new Promise((resolve) => queueMicrotask(resolve));
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("WorkerFetchClient", () => {
  let worker: MockWorker;
  let client: WorkerFetchClient;

  beforeEach(() => {
    worker = new MockWorker();
    client = createClient(worker);
  });

  // ─── Basic round-trips ────────────────────────────────────────────────────

  it("GET: sends a request message and resolves with the worker result", async () => {
    const promise = client.get<{ id: number }>("/api/v2/items");
    await tick();

    const req = worker.sent[0];
    expect(req.type).toBe("request");
    if (req.type !== "request") return;
    expect(req.method).toBe("GET");
    expect(req.path).toBe("/api/v2/items");
    expect(req.kind).toBe("json");

    worker.reply({ type: "response", id: req.id, result: { id: 1 } });
    expect(await promise).toEqual({ id: 1 });
  });

  it("POST: sends body and resolves with result", async () => {
    const body = { name: "test" };
    const promise = client.post<{ created: boolean }>("/api/v2/items", body);
    await tick();

    const req = worker.sent[0];
    expect(req.type).toBe("request");
    if (req.type !== "request") return;
    expect(req.method).toBe("POST");
    expect(req.body).toEqual(body);

    worker.reply({ type: "response", id: req.id, result: { created: true } });
    expect(await promise).toEqual({ created: true });
  });

  it("error response rejects with the worker error message", async () => {
    const promise = client.get("/api/v2/items");
    await tick();

    const req = worker.sent[0];
    if (req.type !== "request") return;
    worker.reply({ type: "error", id: req.id, message: "HTTP 404" });

    let caught: Error | null = null;
    try { await promise; } catch (e) { caught = e as Error; }
    expect(caught?.message).toBe("HTTP 404");
  });

  // ─── Correlation ID isolation ─────────────────────────────────────────────

  it("concurrent requests resolve to their own responses (no cross-wiring)", async () => {
    const p1 = client.get<string>("/api/v2/a");
    const p2 = client.get<string>("/api/v2/b");
    await tick();

    const [req1, req2] = worker.sent as [ToWorkerMessage & { type: "request" }, ToWorkerMessage & { type: "request" }];
    expect(req1.path).toBe("/api/v2/a");
    expect(req2.path).toBe("/api/v2/b");
    expect(req1.id).not.toBe(req2.id);

    // Reply in reverse order
    worker.reply({ type: "response", id: req2.id, result: "B" });
    worker.reply({ type: "response", id: req1.id, result: "A" });

    expect(await p1).toBe("A");
    expect(await p2).toBe("B");
  });

  // ─── GET deduplication (Issue 14A) ───────────────────────────────────────

  it("concurrent identical GET requests share one in-flight promise", async () => {
    const p1 = client.get("/api/v2/same");
    const p2 = client.get("/api/v2/same");
    await tick();

    // Only ONE request message sent to worker
    const requests = worker.sent.filter((m) => m.type === "request");
    expect(requests.length).toBe(1);

    const req = requests[0];
    if (req.type !== "request") return;
    worker.reply({ type: "response", id: req.id, result: "deduped" });

    expect(await p1).toBe("deduped");
    expect(await p2).toBe("deduped");
  });

  it("sequential GET requests (after first resolves) are not deduplicated", async () => {
    const p1 = client.get("/api/v2/same");
    await tick();
    const req1 = worker.sent[0];
    if (req1.type !== "request") return;
    worker.reply({ type: "response", id: req1.id, result: "first" });
    await p1;

    const p2 = client.get("/api/v2/same");
    await tick();
    expect(worker.sent.filter((m) => m.type === "request").length).toBe(2);
    const req2 = worker.sent[1];
    if (req2.type !== "request") return;
    worker.reply({ type: "response", id: req2.id, result: "second" });
    expect(await p2).toBe("second");
  });

  // ─── Timeout (Issue 7A) ───────────────────────────────────────────────────

  it("request times out and sends cancel message to worker", async () => {
    const promise = client.get("/api/v2/slow", { timeout: 10 });
    await tick();

    const req = worker.sent[0];
    if (req.type !== "request") return;

    // Wait for the timeout to fire
    await new Promise((resolve) => setTimeout(resolve, 50));

    let caught: Error | null = null;
    try { await promise; } catch (e) { caught = e as Error; }
    expect(caught?.message).toMatch(/timeout/i);

    const cancelMsg = worker.sent.find((m) => m.type === "cancel");
    expect(cancelMsg).toBeDefined();
    if (cancelMsg?.type !== "cancel") return;
    expect(cancelMsg.id).toBe(req.id);
  });

  it("late worker response after timeout is silently dropped (no double-resolve)", async () => {
    const results: unknown[] = [];
    const promise = client
      .get("/api/v2/slow", { timeout: 10 })
      .then((v) => results.push(v))
      .catch(() => {}); // suppress unhandled rejection
    await tick();

    const req = worker.sent[0];
    if (req.type !== "request") return;

    await new Promise((resolve) => setTimeout(resolve, 50));
    await promise;

    // Late response arrives after timeout — should be silently dropped
    worker.reply({ type: "response", id: req.id, result: "late" });
    await tick();
    expect(results).toHaveLength(0);
  });

  // ─── AbortSignal (Issue 16A) ──────────────────────────────────────────────

  it("AbortSignal abort sends cancel to worker and rejects the promise", async () => {
    const controller = new AbortController();
    const promise = client.get("/api/v2/item", { signal: controller.signal });
    await tick();

    const req = worker.sent[0];
    if (req.type !== "request") return;

    controller.abort();
    // No tick between abort and await — let the microtask queue handle rejection
    // inside the await rather than flagging it as unhandled between ticks.
    let caught: Error | null = null;
    try { await promise; } catch (e) { caught = e as Error; }
    expect(caught?.message).toMatch(/aborted/i);

    const cancelMsg = worker.sent.find((m) => m.type === "cancel");
    expect(cancelMsg).toBeDefined();
  });

  it("already-aborted signal rejects immediately without sending request", async () => {
    const controller = new AbortController();
    controller.abort();

    const promise = client.get("/api/v2/item", { signal: controller.signal });
    let caught: Error | null = null;
    try { await promise; } catch (e) { caught = e as Error; }
    expect(caught?.message).toMatch(/aborted/i);
    expect(worker.sent.filter((m) => m.type === "request")).toHaveLength(0);
  });

  // ─── readyPromise gate (Issue 15A) ───────────────────────────────────────

  it("requests queue until worker signals ready", async () => {
    const notReadyWorker = new MockWorker();
    const notReadyClient = createClient(notReadyWorker, false);

    let resolved = false;
    const getPromise = notReadyClient.get<string>("/api/v2/item").then((v) => {
      resolved = true;
      return v;
    });
    // Suppress unhandled rejection from the default 30s timeout
    getPromise.catch(() => {});

    await tick();

    // No request sent yet — worker not ready
    expect(notReadyWorker.sent.filter((m) => m.type === "request")).toHaveLength(0);
    expect(resolved).toBe(false);

    // Signal ready — triggers the queued send()
    notReadyWorker.dispatchReady();
    // Allow: ready promise resolve → send() continuation → postMessage
    await tick(); await tick(); await tick();

    const requests = notReadyWorker.sent.filter((m) => m.type === "request");
    expect(requests.length).toBe(1);
    const req = requests[0];
    if (req.type !== "request") return;

    notReadyWorker.reply({ type: "response", id: req.id, result: "ok" });
    await tick(); await tick(); await tick();
    expect(resolved).toBe(true);
  });

  // ─── void operations ─────────────────────────────────────────────────────

  it("postNoContent resolves with undefined", async () => {
    const promise = client.postNoContent("/api/v2/actions/run", { trigger: "now" });
    await tick();

    const req = worker.sent[0];
    if (req.type !== "request") return;
    expect(req.kind).toBe("void");
    expect(req.method).toBe("POST");

    worker.reply({ type: "response", id: req.id, result: null });
    await expect(promise).resolves.toBeUndefined();
  });

  it("deleteNoContent resolves with undefined", async () => {
    const promise = client.deleteNoContent("/api/v2/locks/abc");
    await tick();

    const req = worker.sent[0];
    if (req.type !== "request") return;
    expect(req.kind).toBe("void");
    expect(req.method).toBe("DELETE");

    worker.reply({ type: "response", id: req.id, result: null });
    await expect(promise).resolves.toBeUndefined();
  });

  // ─── rawRequest ───────────────────────────────────────────────────────────

  it("rawRequest returns a reconstructed Response with correct status and headers", async () => {
    const promise = client.rawRequest("HEAD", "/api/v2/uploads/session-1");
    await tick();

    const req = worker.sent[0];
    if (req.type !== "request") return;
    expect(req.kind).toBe("raw");

    worker.reply({
      type: "raw-response",
      id: req.id,
      result: {
        status: 200,
        statusText: "OK",
        headers: [["Upload-Offset", "1024"], ["Upload-Length", "4096"]],
        body: "",
        ok: true,
      },
    });

    const response = await promise;
    expect(response.status).toBe(200);
    expect(response.ok).toBe(true);
    expect(response.headers.get("Upload-Offset")).toBe("1024");
    expect(response.headers.get("Upload-Length")).toBe("4096");
  });

  // ─── _rewire (crash recovery) ─────────────────────────────────────────────

  it("_rewire: after rewire, messages go to new worker", async () => {
    const newWorker = new MockWorker();
    const newReady = Promise.resolve();
    client._rewire(newWorker as unknown as Worker, newReady);

    const promise = client.get("/api/v2/items");
    await tick();

    expect(worker.sent.filter((m) => m.type === "request")).toHaveLength(0);
    expect(newWorker.sent.filter((m) => m.type === "request")).toHaveLength(1);

    const req = newWorker.sent[0];
    if (req.type !== "request") return;
    newWorker.reply({ type: "response", id: req.id, result: "rewired" });
    expect(await promise).toBe("rewired");
  });

  // ─── _rejectAll (crash recovery) ──────────────────────────────────────────

  it("_rejectAll: rejects all in-flight requests with the given error", async () => {
    const p1 = client.get("/api/v2/a");
    const p2 = client.get("/api/v2/b");
    await tick();

    client._rejectAll(new Error("Worker crashed"));

    let e1: Error | null = null, e2: Error | null = null;
    try { await p1; } catch (e) { e1 = e as Error; }
    try { await p2; } catch (e) { e2 = e as Error; }
    expect(e1?.message).toBe("Worker crashed");
    expect(e2?.message).toBe("Worker crashed");
  });
});
