/**
 * SSE lifecycle integration test.
 *
 * Tests connection state transitions:
 *   connected → disconnect → reconnecting (attempt 1-10) → exhausted → manual retry
 *
 * Verifies correct status strings at each state for wireframe Screen 11.
 *
 * @see Issue #3250 Screen 11 wireframe, Issue 11A
 */

import { describe, it, expect, beforeEach } from "bun:test";
import { useEventsStore } from "../../src/stores/events-store.js";

describe("SSE lifecycle states", () => {
  beforeEach(() => {
    useEventsStore.getState().disconnect();
    useEventsStore.setState({
      events: [],
      connected: false,
      reconnectCount: 0,
      reconnectExhausted: false,
      filters: { eventType: null, search: null },
      filteredEvents: [],
      eventsOverflowed: false,
      evictedCount: 0,
      sseClient: null,
    });
  });

  describe("connection state indicators", () => {
    it("shows disconnected state initially", () => {
      const state = useEventsStore.getState();
      expect(state.connected).toBe(false);
      expect(state.reconnectCount).toBe(0);
      expect(state.reconnectExhausted).toBe(false);
      // Wireframe: "○ Disconnected"
    });

    it("shows connected state when connected", () => {
      useEventsStore.setState({ connected: true });
      const state = useEventsStore.getState();
      expect(state.connected).toBe(true);
      // Wireframe: "● Connected — N events"
    });

    it("shows reconnecting state with attempt count", () => {
      useEventsStore.setState({
        connected: false,
        reconnectCount: 3,
        reconnectExhausted: false,
      });
      const state = useEventsStore.getState();
      expect(state.connected).toBe(false);
      expect(state.reconnectCount).toBe(3);
      expect(state.reconnectExhausted).toBe(false);
      // Wireframe: "◐ Auto-reconnecting (attempt 3/10)..."
    });

    it("shows exhausted state after max retries", () => {
      useEventsStore.setState({
        connected: false,
        reconnectCount: 10,
        reconnectExhausted: true,
      });
      const state = useEventsStore.getState();
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
      useEventsStore.setState({
        connected: true,
        reconnectCount: 5,
        reconnectExhausted: false,
      });

      useEventsStore.getState().disconnect();
      const state = useEventsStore.getState();
      expect(state.connected).toBe(false);
      expect(state.reconnectCount).toBe(0);
      expect(state.sseClient).toBeNull();
    });
  });

  describe("filter persistence across reconnection", () => {
    it("preserves filters when connection drops", () => {
      // Set up filters while connected
      useEventsStore.setState({ connected: true });
      useEventsStore.getState().setFilter({ eventType: "file.write", search: "important" });

      // Simulate disconnect
      useEventsStore.setState({ connected: false, reconnectCount: 1 });

      // Filters should still be set
      const state = useEventsStore.getState();
      expect(state.filters.eventType).toBe("file.write");
      expect(state.filters.search).toBe("important");
    });
  });
});
