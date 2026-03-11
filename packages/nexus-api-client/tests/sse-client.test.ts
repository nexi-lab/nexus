import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
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
});

describe("SseClient", () => {
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
});
