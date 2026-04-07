import { describe, it, expect, beforeEach } from "bun:test";
import { useEventsStore } from "../../src/stores/events-store.js";

describe("EventsStore", () => {
  beforeEach(() => {
    useEventsStore.getState().eventsBuffer.clear();
    useEventsStore.setState({
      events: [],
      filters: { eventType: null, search: null },
      filteredEvents: [],
      eventsOverflowed: false,
      evictedCount: 0,
    });
  });

  describe("initial state", () => {
    it("starts with empty events", () => {
      const state = useEventsStore.getState();
      expect(state.events).toEqual([]);
    });
  });

  describe("setFilter", () => {
    it("filters by event type", () => {
      useEventsStore.setState({
        events: [
          { event: "file.write", data: '{"path":"/a"}' },
          { event: "file.delete", data: '{"path":"/b"}' },
          { event: "file.write", data: '{"path":"/c"}' },
        ],
      });

      useEventsStore.getState().setFilter({ eventType: "file.write" });
      expect(useEventsStore.getState().filteredEvents.length).toBe(2);
    });

    it("filters by search string", () => {
      useEventsStore.setState({
        events: [
          { event: "file.write", data: '{"path":"/important/doc.txt"}' },
          { event: "file.write", data: '{"path":"/other/stuff.txt"}' },
        ],
      });

      useEventsStore.getState().setFilter({ search: "important" });
      expect(useEventsStore.getState().filteredEvents.length).toBe(1);
    });

    it("combines filters", () => {
      useEventsStore.setState({
        events: [
          { event: "file.write", data: '{"path":"/a"}' },
          { event: "file.delete", data: '{"path":"/a"}' },
          { event: "file.write", data: '{"path":"/b"}' },
        ],
      });

      useEventsStore.getState().setFilter({ eventType: "file.write", search: "/a" });
      expect(useEventsStore.getState().filteredEvents.length).toBe(1);
    });
  });

  describe("clearEvents", () => {
    it("clears events and filtered events", () => {
      useEventsStore.setState({
        events: [{ event: "test", data: "data" }],
        filteredEvents: [{ event: "test", data: "data" }],
      });

      useEventsStore.getState().clearEvents();
      expect(useEventsStore.getState().events).toEqual([]);
      expect(useEventsStore.getState().filteredEvents).toEqual([]);
    });
  });
});
