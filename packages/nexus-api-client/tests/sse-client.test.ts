import { describe, it, expect, vi, afterEach } from "vitest";
import { RingBuffer, SseClient } from "../src/sse-client.js";

describe("RingBuffer", () => {
  it("stores items up to capacity", () => {
    const buf = new RingBuffer<number>(3);
    buf.push(1);
    buf.push(2);
    buf.push(3);
    expect(buf.size).toBe(3);
    expect(buf.toArray()).toEqual([1, 2, 3]);
  });

  it("drops oldest when full", () => {
    const buf = new RingBuffer<number>(3);
    buf.push(1);
    buf.push(2);
    buf.push(3);
    buf.push(4);
    expect(buf.size).toBe(3);
    expect(buf.toArray()).toEqual([2, 3, 4]);
  });

  it("wraps around correctly", () => {
    const buf = new RingBuffer<number>(3);
    for (let i = 1; i <= 6; i++) {
      buf.push(i);
    }
    expect(buf.toArray()).toEqual([4, 5, 6]);
  });

  it("returns empty array when empty", () => {
    const buf = new RingBuffer<string>(5);
    expect(buf.size).toBe(0);
    expect(buf.toArray()).toEqual([]);
  });

  it("clears correctly", () => {
    const buf = new RingBuffer<number>(3);
    buf.push(1);
    buf.push(2);
    buf.clear();
    expect(buf.size).toBe(0);
    expect(buf.toArray()).toEqual([]);
  });

  it("works with capacity 1", () => {
    const buf = new RingBuffer<string>(1);
    buf.push("a");
    expect(buf.toArray()).toEqual(["a"]);
    buf.push("b");
    expect(buf.toArray()).toEqual(["b"]);
  });

  it("rejects invalid capacities", () => {
    expect(() => new RingBuffer<number>(0)).toThrow(RangeError);
    expect(() => new RingBuffer<number>(-1)).toThrow(RangeError);
    expect(() => new RingBuffer<number>(1.5)).toThrow(RangeError);
  });

  it("tracks totalPushed monotonically", () => {
    const buf = new RingBuffer<number>(2);
    expect(buf.totalPushed).toBe(0);
    buf.push(1);
    expect(buf.totalPushed).toBe(1);
    buf.push(2);
    buf.push(3); // overflows capacity
    expect(buf.totalPushed).toBe(3);
    expect(buf.size).toBe(2);
  });

  it("resets totalPushed on clear", () => {
    const buf = new RingBuffer<number>(3);
    buf.push(1);
    buf.push(2);
    expect(buf.totalPushed).toBe(2);
    buf.clear();
    expect(buf.totalPushed).toBe(0);
  });

  it("lastN returns last N items in insertion order", () => {
    const buf = new RingBuffer<number>(5);
    buf.push(10);
    buf.push(20);
    buf.push(30);
    buf.push(40);
    expect(buf.lastN(2)).toEqual([30, 40]);
    expect(buf.lastN(4)).toEqual([10, 20, 30, 40]);
  });

  it("lastN clamps to available items", () => {
    const buf = new RingBuffer<number>(5);
    buf.push(1);
    buf.push(2);
    expect(buf.lastN(10)).toEqual([1, 2]);
  });

  it("lastN returns empty for n <= 0 or empty buffer", () => {
    const buf = new RingBuffer<number>(5);
    expect(buf.lastN(0)).toEqual([]);
    expect(buf.lastN(-1)).toEqual([]);
    expect(buf.lastN(1)).toEqual([]);
  });
});

/**
 * Helper: create a mock fetch that returns an SSE stream on first call,
 * then throws AbortError (to exit connectWithRetry loop) on subsequent calls.
 * The clientRef is disconnected after the stream data is consumed.
 */
function mockSseFetch(
  chunks: string[],
  clientRef: { current: SseClient | null },
): typeof globalThis.fetch {
  let callCount = 0;
  return vi.fn(async (_url: string, init?: RequestInit) => {
    callCount++;
    if (callCount > 1 || init?.signal?.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    const encoder = new TextEncoder();
    let i = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (i < chunks.length) {
          controller.enqueue(encoder.encode(chunks[i]!));
          i++;
        } else {
          controller.close();
          // Disconnect after stream is consumed to break reconnect loop
          setTimeout(() => clientRef.current?.disconnect(), 0);
        }
      },
    });
    return new Response(stream, { status: 200 });
  }) as unknown as typeof globalThis.fetch;
}

describe("SseClient", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("tracks connection state", () => {
    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
    });
    expect(client.isConnected).toBe(false);
  });

  it("clears buffer", () => {
    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
    });
    expect(client.getBufferedEvents()).toEqual([]);
    client.clearBuffer();
    expect(client.getBufferedEvents()).toEqual([]);
  });

  it("disconnect is safe to call when not connected", () => {
    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
    });
    expect(() => client.disconnect()).not.toThrow();
  });

  it("registers event handlers", () => {
    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
    });
    const handler = vi.fn();
    const errorHandler = vi.fn();
    const reconnectHandler = vi.fn();

    client.onEvent(handler);
    client.onError(errorHandler);
    client.onReconnect(reconnectHandler);
    // No throw — handlers registered successfully
  });

  it("parses SSE events from stream and buffers them", async () => {
    const clientRef: { current: SseClient | null } = { current: null };
    const fetchFn = mockSseFetch(
      ["event: update\ndata: hello\n\n", "id: 42\nevent: msg\ndata: world\n\n"],
      clientRef,
    );

    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
      fetch: fetchFn,
      flushIntervalMs: 10_000, // large to avoid flush interference
    });
    clientRef.current = client;

    await client.connect("/events");

    const events = client.getBufferedEvents();
    expect(events).toHaveLength(2);
    expect(events[0]).toEqual({ id: undefined, event: "update", data: "hello", retry: undefined });
    expect(events[1]).toEqual({ id: "42", event: "msg", data: "world", retry: undefined });
  });

  it("parses retry field in SSE events", async () => {
    const clientRef: { current: SseClient | null } = { current: null };
    const fetchFn = mockSseFetch(
      ["event: ping\ndata: test\nretry: 5000\n\n"],
      clientRef,
    );

    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
      fetch: fetchFn,
      flushIntervalMs: 10_000,
    });
    clientRef.current = client;

    await client.connect("/events");

    const events = client.getBufferedEvents();
    expect(events).toHaveLength(1);
    expect(events[0]!.retry).toBe(5000);
  });

  it("handles chunked SSE data split across reads", async () => {
    const clientRef: { current: SseClient | null } = { current: null };
    const fetchFn = mockSseFetch(
      ["event: split\nda", "ta: partial\n\n"],
      clientRef,
    );

    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
      fetch: fetchFn,
      flushIntervalMs: 10_000,
    });
    clientRef.current = client;

    await client.connect("/events");

    const events = client.getBufferedEvents();
    expect(events).toHaveLength(1);
    expect(events[0]!.event).toBe("split");
    expect(events[0]!.data).toBe("partial");
  });

  it("handles multi-line data fields", async () => {
    const clientRef: { current: SseClient | null } = { current: null };
    const fetchFn = mockSseFetch(
      ["data: line1\ndata: line2\n\n"],
      clientRef,
    );

    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
      fetch: fetchFn,
      flushIntervalMs: 10_000,
    });
    clientRef.current = client;

    await client.connect("/events");

    const events = client.getBufferedEvents();
    expect(events).toHaveLength(1);
    expect(events[0]!.data).toBe("line1\nline2");
  });

  it("parses CRLF-terminated SSE events", async () => {
    const clientRef: { current: SseClient | null } = { current: null };
    const fetchFn = mockSseFetch(
      ["id: 7\r\nevent: update\r\ndata: hello\r\n\r\n"],
      clientRef,
    );

    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
      fetch: fetchFn,
      flushIntervalMs: 10_000,
    });
    clientRef.current = client;

    await client.connect("/events");

    const events = client.getBufferedEvents();
    expect(events).toHaveLength(1);
    expect(events[0]).toEqual({ id: "7", event: "update", data: "hello", retry: undefined });
  });

  it("skips empty SSE blocks", async () => {
    const clientRef: { current: SseClient | null } = { current: null };
    const fetchFn = mockSseFetch(
      ["\n\ndata: real\n\n\n\n"],
      clientRef,
    );

    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
      fetch: fetchFn,
      flushIntervalMs: 10_000,
    });
    clientRef.current = client;

    await client.connect("/events");

    const events = client.getBufferedEvents();
    expect(events).toHaveLength(1);
    expect(events[0]!.data).toBe("real");
  });

  it("sends auth and identity headers", async () => {
    const clientRef: { current: SseClient | null } = { current: null };
    const fetchFn = mockSseFetch(["data: x\n\n"], clientRef);

    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "my-key",
      agentId: "agent-1",
      subject: "user:alice",
      zoneId: "zone-abc",
      fetch: fetchFn,
      flushIntervalMs: 10_000,
    });
    clientRef.current = client;

    await client.connect("/events");

    const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(init.headers.Authorization).toBe("Bearer my-key");
    expect(init.headers["X-Agent-ID"]).toBe("agent-1");
    expect(init.headers["X-Nexus-Subject"]).toBe("user:alice");
    expect(init.headers["X-Nexus-Zone-ID"]).toBe("zone-abc");
  });

  it("calls error handler on failed HTTP response", async () => {
    let callCount = 0;
    const fetchFn = vi.fn(async (_url: string, init?: RequestInit) => {
      callCount++;
      if (callCount > 1 || init?.signal?.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }
      return new Response("Forbidden", { status: 403 });
    }) as unknown as typeof globalThis.fetch;

    const errorHandler = vi.fn();
    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
      fetch: fetchFn,
    });
    client.onError(errorHandler);

    // connect will fail, try to reconnect, then get AbortError and exit
    const connectPromise = client.connect("/events");
    // Wait a tick for the error handler to fire, then disconnect
    await new Promise((r) => setTimeout(r, 50));
    client.disconnect();
    await connectPromise;

    expect(errorHandler).toHaveBeenCalled();
    const err = errorHandler.mock.calls[0]![0] as Error;
    expect(err.message).toContain("403");
  });

  it("calls error handler when response has no body", async () => {
    let callCount = 0;
    const fetchFn = vi.fn(async (_url: string, init?: RequestInit) => {
      callCount++;
      if (callCount > 1 || init?.signal?.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }
      return { ok: true, body: null, status: 200 };
    }) as unknown as typeof globalThis.fetch;

    const errorHandler = vi.fn();
    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
      fetch: fetchFn,
    });
    client.onError(errorHandler);

    const connectPromise = client.connect("/events");
    await new Promise((r) => setTimeout(r, 50));
    client.disconnect();
    await connectPromise;

    expect(errorHandler).toHaveBeenCalled();
    const err = errorHandler.mock.calls[0]![0] as Error;
    expect(err.message).toContain("no body");
  });

  it("fires reconnect handler on connection failure", async () => {
    let callCount = 0;
    const fetchFn = vi.fn(async (_url: string, init?: RequestInit) => {
      callCount++;
      if (init?.signal?.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }
      throw new Error("Connection refused");
    }) as unknown as typeof globalThis.fetch;

    const reconnectHandler = vi.fn();
    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
      fetch: fetchFn,
    });
    client.onReconnect(reconnectHandler);

    const connectPromise = client.connect("/events");
    // Wait enough for first failure + first reconnect attempt
    await new Promise((r) => setTimeout(r, 700));
    client.disconnect();
    await connectPromise;

    expect(reconnectHandler).toHaveBeenCalledWith(1);
  });

  it("fires reconnect handler when stream closes cleanly", async () => {
    let callCount = 0;
    const fetchFn = vi.fn(async (_url: string, init?: RequestInit) => {
      callCount++;
      if (init?.signal?.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }
      // Return an empty stream that closes immediately.
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.close();
          },
        }),
        { status: 200 },
      );
    }) as unknown as typeof globalThis.fetch;

    const reconnectHandler = vi.fn();
    const client = new SseClient({
      baseUrl: "http://localhost:2026",
      apiKey: "test-key",
      fetch: fetchFn,
    });
    client.onReconnect(reconnectHandler);

    const connectPromise = client.connect("/events");
    await new Promise((r) => setTimeout(r, 700));
    client.disconnect();
    await connectPromise;

    expect(reconnectHandler).toHaveBeenCalledWith(1);
  });

  it("strips trailing slashes from baseUrl", async () => {
    const clientRef: { current: SseClient | null } = { current: null };
    const fetchFn = mockSseFetch(["data: x\n\n"], clientRef);

    const client = new SseClient({
      baseUrl: "http://localhost:2026///",
      apiKey: "test-key",
      fetch: fetchFn,
      flushIntervalMs: 10_000,
    });
    clientRef.current = client;

    await client.connect("/events");

    const [url] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
    expect(url).toBe("http://localhost:2026/events");
  });
});
