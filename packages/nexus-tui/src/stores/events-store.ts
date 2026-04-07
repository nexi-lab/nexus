/**
 * Zustand store for the Events panel display.
 *
 * Consumes events from the shared SSE bus (sse-bus.ts) rather than
 * managing its own SseClient connection. Maintains a CircularBuffer
 * for bounded display and provides filtering.
 *
 * @see Issue #3632 §3 — SSE event streaming for live panel updates
 */

import { createStore as create } from "./create-store.js";
import type { SseEvent } from "@nexus-ai-fs/api-client";
import { CircularBuffer } from "../shared/lib/circular-buffer.js";
import { useSseBus } from "./sse-bus.js";

const EVENTS_BUFFER_CAPACITY = 10_000;

export interface EventFilters {
  readonly eventType: string | null;
  readonly search: string | null;
}

export interface EventsState {
  readonly events: readonly SseEvent[];
  readonly filters: EventFilters;
  readonly filteredEvents: readonly SseEvent[];
  readonly eventsOverflowed: boolean;
  readonly evictedCount: number;

  // Internal circular buffer (not serializable, but that's fine for Zustand)
  readonly eventsBuffer: CircularBuffer<SseEvent>;

  // Actions
  readonly setFilter: (filters: Partial<EventFilters>) => void;
  readonly clearEvents: () => void;
}

export const useEventsStore = create<EventsState>((set, get) => ({
  events: [],
  filters: { eventType: null, search: null },
  filteredEvents: [],
  eventsOverflowed: false,
  evictedCount: 0,
  eventsBuffer: new CircularBuffer<SseEvent>(EVENTS_BUFFER_CAPACITY),

  setFilter: (partial) => {
    set((state) => {
      const filters = { ...state.filters, ...partial };
      return {
        filters,
        filteredEvents: applyFilters(state.events, filters),
      };
    });
  },

  clearEvents: () => {
    get().eventsBuffer.clear();
    set({ events: [], filteredEvents: [], eventsOverflowed: false, evictedCount: 0 });
  },
}));

function applyFilters(
  events: readonly SseEvent[],
  filters: EventFilters,
): readonly SseEvent[] {
  let result = events;

  if (filters.eventType) {
    const type = filters.eventType;
    result = result.filter((e) => e.event === type);
  }

  if (filters.search) {
    const lower = filters.search.toLowerCase();
    result = result.filter((e) => e.data.toLowerCase().includes(lower));
  }

  return result;
}

// =============================================================================
// SSE bus handler — feed all raw events into the display buffer
// =============================================================================

useSseBus.getState().registerHandler("events", (parsedEvents) => {
  queueMicrotask(() => {
    useEventsStore.setState((state) => {
      const buf = state.eventsBuffer;
      for (const pe of parsedEvents) {
        buf.push(pe.raw);
      }
      const allEvents = buf.toArray();
      return {
        events: allEvents,
        filteredEvents: applyFilters(allEvents, state.filters),
        eventsOverflowed: buf.hasOverflowed,
        evictedCount: buf.evictedCount,
      };
    });
  });
}, { debounceMs: 0 }); // No debounce — events panel shows real-time stream
