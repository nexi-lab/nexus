/**
 * Unit tests for the SSE bus store.
 *
 * @see Issue #3632 §3 — SSE event streaming for live panel updates
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { useSseBus, _testInternals, type ParsedEvent } from "../../src/stores/sse-bus.js";

const { handlers, parseEvent, dispatch, clearAllTimers } = _testInternals;

describe("SseBus", () => {
  beforeEach(() => {
    useSseBus.getState().disconnect();
    // Clean up all handlers
    for (const id of [...handlers.keys()]) {
      // Don't remove domain handlers registered at module load — only remove test ones
      if (id.startsWith("test:")) {
        useSseBus.getState().unregisterHandler(id);
      }
    }
    useSseBus.setState({
      connected: false,
      reconnectCount: 0,
      reconnectExhausted: false,
    });
  });

  afterEach(() => {
    for (const id of [...handlers.keys()]) {
      if (id.startsWith("test:")) {
        useSseBus.getState().unregisterHandler(id);
      }
    }
    clearAllTimers();
  });

  describe("initial state", () => {
    it("starts disconnected", () => {
      const state = useSseBus.getState();
      expect(state.connected).toBe(false);
      expect(state.reconnectCount).toBe(0);
      expect(state.reconnectExhausted).toBe(false);
    });
  });

  describe("parseEvent", () => {
    it("parses valid JSON data into ParsedEvent", () => {
      const raw = { event: "event", data: '{"type":"write","path":"/a.txt","zone_id":"z1","agent_id":"a1"}' };
      const result = parseEvent(raw);
      expect(result).not.toBeNull();
      expect(result!.type).toBe("write");
      expect(result!.path).toBe("/a.txt");
      expect(result!.zoneId).toBe("z1");
      expect(result!.agentId).toBe("a1");
      expect(result!.raw).toBe(raw);
    });

    it("returns 'unknown' type for JSON without type field", () => {
      const raw = { event: "event", data: '{"path":"/b.txt"}' };
      const result = parseEvent(raw);
      expect(result).not.toBeNull();
      expect(result!.type).toBe("unknown");
      expect(result!.path).toBe("/b.txt");
    });

    it("handles malformed JSON gracefully", () => {
      const raw = { event: "event", data: "not json" };
      const result = parseEvent(raw);
      expect(result).not.toBeNull();
      expect(result!.type).toBe("unknown");
      expect(result!.payload).toEqual({});
    });

    it("returns null for empty data", () => {
      const raw = { event: "event", data: "" };
      const result = parseEvent(raw);
      expect(result).toBeNull();
    });
  });

  describe("registerHandler / unregisterHandler", () => {
    it("registers and invokes a handler", () => {
      const received: ParsedEvent[][] = [];
      useSseBus.getState().registerHandler("test:a", (events) => {
        received.push([...events]);
      }, { debounceMs: 0 });

      const parsed = parseEvent({ event: "event", data: '{"type":"write","path":"/x"}' })!;
      dispatch(parsed);

      expect(received.length).toBe(1);
      expect(received[0]![0]!.type).toBe("write");
    });

    it("unregisters a handler", () => {
      const received: ParsedEvent[][] = [];
      useSseBus.getState().registerHandler("test:b", (events) => {
        received.push([...events]);
      }, { debounceMs: 0 });

      useSseBus.getState().unregisterHandler("test:b");

      const parsed = parseEvent({ event: "event", data: '{"type":"delete"}' })!;
      dispatch(parsed);

      expect(received.length).toBe(0);
    });

    it("dispatches to multiple handlers", () => {
      let countA = 0;
      let countB = 0;
      useSseBus.getState().registerHandler("test:c", () => { countA++; }, { debounceMs: 0 });
      useSseBus.getState().registerHandler("test:d", () => { countB++; }, { debounceMs: 0 });

      const parsed = parseEvent({ event: "event", data: '{"type":"write"}' })!;
      dispatch(parsed);

      expect(countA).toBe(1);
      expect(countB).toBe(1);
    });
  });

  describe("debounce", () => {
    it("batches events within debounce window", async () => {
      const received: ParsedEvent[][] = [];
      useSseBus.getState().registerHandler("test:debounce", (events) => {
        received.push([...events]);
      }, { debounceMs: 50 });

      // Dispatch 3 events rapidly
      dispatch(parseEvent({ event: "event", data: '{"type":"write","path":"/1"}' })!);
      dispatch(parseEvent({ event: "event", data: '{"type":"write","path":"/2"}' })!);
      dispatch(parseEvent({ event: "event", data: '{"type":"write","path":"/3"}' })!);

      // Should not have been called yet
      expect(received.length).toBe(0);

      // Wait for debounce to flush
      await new Promise((r) => setTimeout(r, 80));

      // Should receive all 3 events in a single batch
      expect(received.length).toBe(1);
      expect(received[0]!.length).toBe(3);
    });

    it("flushes immediately when debounceMs is 0", () => {
      const received: ParsedEvent[][] = [];
      useSseBus.getState().registerHandler("test:nodelay", (events) => {
        received.push([...events]);
      }, { debounceMs: 0 });

      dispatch(parseEvent({ event: "event", data: '{"type":"write"}' })!);
      dispatch(parseEvent({ event: "event", data: '{"type":"delete"}' })!);

      // Each dispatch flushes immediately (no batching)
      expect(received.length).toBe(2);
    });
  });

  describe("disconnect", () => {
    it("resets connection state", () => {
      useSseBus.setState({
        connected: true,
        reconnectCount: 5,
        reconnectExhausted: false,
      });

      useSseBus.getState().disconnect();
      const state = useSseBus.getState();
      expect(state.connected).toBe(false);
      expect(state.reconnectCount).toBe(0);
      expect(state.reconnectExhausted).toBe(false);
    });

    it("clears pending debounce timers", async () => {
      const received: ParsedEvent[][] = [];
      useSseBus.getState().registerHandler("test:disconnect", (events) => {
        received.push([...events]);
      }, { debounceMs: 100 });

      dispatch(parseEvent({ event: "event", data: '{"type":"write"}' })!);

      // Disconnect before debounce fires
      useSseBus.getState().disconnect();

      await new Promise((r) => setTimeout(r, 150));

      // Handler should NOT have been called (timer cleared)
      expect(received.length).toBe(0);
    });
  });

  describe("reconnect", () => {
    it("does nothing when no previous connection params", () => {
      // reconnect without prior connect should not throw
      useSseBus.getState().reconnect();
      expect(useSseBus.getState().connected).toBe(false);
    });
  });
});
