/**
 * Zustand store for real-time SSE events.
 */

import { create } from "zustand";
import type { SseEvent } from "@nexus/api-client";
import { SseClient } from "@nexus/api-client";
import { CircularBuffer } from "../shared/lib/circular-buffer.js";

const EVENTS_BUFFER_CAPACITY = 10_000;

export interface EventFilters {
  readonly eventType: string | null;
  readonly search: string | null;
}

export interface SseIdentity {
  readonly agentId?: string;
  readonly subject?: string;
  readonly zoneId?: string;
}

export interface EventsState {
  readonly events: readonly SseEvent[];
  readonly connected: boolean;
  readonly reconnectCount: number;
  readonly filters: EventFilters;
  readonly filteredEvents: readonly SseEvent[];
  readonly eventsOverflowed: boolean;
  readonly evictedCount: number;

  // Internal circular buffer (not serializable, but that's fine for Zustand)
  readonly eventsBuffer: CircularBuffer<SseEvent>;

  // SSE client instance (not serializable, but that's fine for Zustand)
  readonly sseClient: SseClient | null;

  // Actions
  readonly connect: (baseUrl: string, apiKey: string, identity?: SseIdentity) => void;
  readonly disconnect: () => void;
  readonly setFilter: (filters: Partial<EventFilters>) => void;
  readonly clearEvents: () => void;
}

export const useEventsStore = create<EventsState>((set, get) => ({
  events: [],
  connected: false,
  reconnectCount: 0,
  filters: { eventType: null, search: null },
  filteredEvents: [],
  eventsOverflowed: false,
  evictedCount: 0,
  eventsBuffer: new CircularBuffer<SseEvent>(EVENTS_BUFFER_CAPACITY),
  sseClient: null,

  connect: (baseUrl, apiKey, identity) => {
    // Disconnect existing
    get().sseClient?.disconnect();

    const client = new SseClient({
      baseUrl,
      apiKey,
      agentId: identity?.agentId,
      subject: identity?.subject,
      zoneId: identity?.zoneId,
    });

    client.onEvent((newEvents) => {
      set((state) => {
        const buf = state.eventsBuffer;
        for (const event of newEvents) {
          buf.push(event);
        }
        const allEvents = buf.toArray();
        return {
          events: allEvents,
          filteredEvents: applyFilters(allEvents, state.filters),
          eventsOverflowed: buf.hasOverflowed,
          evictedCount: buf.evictedCount,
          connected: true, // confirmed connected on first event
        };
      });
    });

    client.onError(() => {
      set({ connected: false });
    });

    client.onReconnect((attempt) => {
      set({ reconnectCount: attempt });
    });

    set({ sseClient: client, connected: false, reconnectCount: 0 });

    // Connect async — don't await (fire and forget)
    client.connect("/api/v2/events/stream").catch(() => {
      set({ connected: false });
    });
  },

  disconnect: () => {
    get().sseClient?.disconnect();
    set({ sseClient: null, connected: false, reconnectCount: 0 });
  },

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
    get().sseClient?.clearBuffer();
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
