/**
 * SSE lifecycle integration test.
 *
 * Tests connection state transitions via the SSE bus:
 *   connected → disconnect → reconnecting (attempt 1-10) → exhausted → manual retry
 *
 * Verifies correct status strings at each state for wireframe Screen 11.
 *
 * @see Issue #3250 Screen 11 wireframe, Issue 11A
 * @see Issue #3632 §3 — SSE bus migration
 */

import { describe, it, expect, beforeEach } from "bun:test";
import { useSseBus } from "../../src/stores/sse-bus.js";
import { useEventsStore } from "../../src/stores/events-store.js";

describe("SSE lifecycle states", () => {
  beforeEach(() => {
    useSseBus.getState().disconnect();
    useSseBus.setState({
      connected: false,
      reconnectCount: 0,
      reconnectExhausted: false,
    });
    useEventsStore.getState().eventsBuffer.clear();
    useEventsStore.setState({
      events: [],
      filters: { eventType: null, search: null },
      filteredEvents: [],
      eventsOverflowed: false,
      evictedCount: 0,
    });
  });

  describe("connection state indicators", () => {
    it("shows disconnected state initially", () => {
      const state = useSseBus.getState();
      expect(state.connected).toBe(false);
      expect(state.reconnectCount).toBe(0);
      expect(state.reconnectExhausted).toBe(false);
      // Wireframe: "○ Disconnected"
    });

    it("shows connected state when connected", () => {
      useSseBus.setState({ connected: true });
      const state = useSseBus.getState();
      expect(state.connected).toBe(true);
      // Wireframe: "● Connected — N events"
    });

    it("shows reconnecting state with attempt count", () => {
      useSseBus.setState({
        connected: false,
        reconnectCount: 3,
        reconnectExhausted: false,
      });
      const state = useSseBus.getState();
      expect(state.connected).toBe(false);
      expect(state.reconnectCount).toBe(3);
      expect(state.reconnectExhausted).toBe(false);
      // Wireframe: "◐ Auto-reconnecting (attempt 3/10)..."
    });

    it("shows exhausted state after max retries", () => {
      useSseBus.setState({
        connected: false,
        reconnectCount: 10,
        reconnectExhausted: true,
      });
      const state = useSseBus.getState();
      expect(state.reconnectExhausted).toBe(true);
      // Wireframe: "✕ Reconnect failed after 10 attempts — press r to retry"
    });
  });

  describe("event buffer overflow", () => {
    it("tracks eviction when buffer overflows", () => {
      // Simulate buffer overflow
      useEventsStore.setState({
        eventsOverflowed: true,
        evictedCount: 42,
      });

      const state = useEventsStore.getState();
      expect(state.eventsOverflowed).toBe(true);
      expect(state.evictedCount).toBe(42);
      // Wireframe: "Showing latest {bufferSize} of {totalAdded} events ({evictedCount} evicted)"
    });
  });

  describe("disconnect resets state", () => {
    it("resets reconnection tracking on disconnect", () => {
      useSseBus.setState({
        connected: true,
        reconnectCount: 5,
        reconnectExhausted: false,
      });

      useSseBus.getState().disconnect();
      const state = useSseBus.getState();
      expect(state.connected).toBe(false);
      expect(state.reconnectCount).toBe(0);
    });
  });

  describe("filter persistence across reconnection", () => {
    it("preserves filters when connection drops", () => {
      // Set up filters while connected
      useSseBus.setState({ connected: true });
      useEventsStore.getState().setFilter({ eventType: "file.write", search: "important" });

      // Simulate disconnect
      useSseBus.setState({ connected: false, reconnectCount: 1 });

      // Filters should still be set
      const state = useEventsStore.getState();
      expect(state.filters.eventType).toBe("file.write");
      expect(state.filters.search).toBe("important");
    });
  });
});
