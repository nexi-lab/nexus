/**
 * Zustand store for knowledge platform data: aspects, schemas, MCL replay.
 * Issue #2930.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";
import { createApiAction, categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";

// =============================================================================
// Types
// =============================================================================

export interface AspectEntry {
  readonly name: string;
  readonly payload: Record<string, unknown>;
  readonly version: number;
  readonly createdBy: string;
}

export interface SchemaColumn {
  readonly name: string;
  readonly type: string;
  readonly nullable: string;
}

export interface SchemaInfo {
  readonly columns: readonly SchemaColumn[];
  readonly format: string;
  readonly rowCount: number | null;
  readonly confidence: number;
}

export interface ReplayEntry {
  readonly sequenceNumber: number;
  readonly entityUrn: string;
  readonly aspectName: string;
  readonly changeType: string;
  readonly timestamp: string;
}

/** Event from the historical event replay endpoint. */
export interface EventReplayEntry {
  readonly event_id: string;
  readonly event_type: string;
  readonly agent_id: string | null;
  readonly path: string | null;
  readonly timestamp: string;
  readonly payload: Record<string, unknown>;
}

// =============================================================================
// Store
// =============================================================================

export interface KnowledgeState {
  // Aspects cache (keyed by URN)
  readonly aspectsCache: ReadonlyMap<string, readonly string[]>;
  readonly aspectDetailCache: ReadonlyMap<string, AspectEntry>;
  readonly aspectsLoading: boolean;
  readonly aspectDetailLoading: boolean;

  // Schema cache (keyed by URN)
  readonly schemaCache: ReadonlyMap<string, SchemaInfo | null>;
  readonly schemaLoading: boolean;

  // MCL replay (bounded by fetch limit parameter, typically 50 per page)
  readonly replayEntries: readonly ReplayEntry[];
  readonly replayLoading: boolean;
  readonly replayHasMore: boolean;
  readonly replayNextCursor: number;

  // Historical event replay
  readonly eventReplayEntries: readonly EventReplayEntry[];
  readonly eventReplayLoading: boolean;
  readonly eventReplayHasMore: boolean;
  readonly eventReplayNextCursor: string | null;

  // Column search
  readonly columnSearchResults: readonly {
    entityUrn: string;
    columnName: string;
    columnType: string;
  }[];
  readonly columnSearchLoading: boolean;

  readonly error: string | null;

  // Actions
  readonly fetchAspects: (urn: string, client: FetchClient) => Promise<void>;
  readonly fetchAspectDetail: (
    urn: string,
    name: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly fetchSchema: (path: string, client: FetchClient) => Promise<void>;
  readonly fetchReplay: (
    client: FetchClient,
    fromSequence?: number,
    limit?: number,
    entityUrn?: string,
    aspectName?: string,
  ) => Promise<void>;
  readonly fetchEventReplay: (
    filters: {
      event_types?: string;
      path_pattern?: string;
      agent_id?: string;
      since?: string;
    },
    client: FetchClient,
  ) => Promise<void>;
  readonly searchByColumn: (
    column: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly clearReplay: () => void;
  readonly clearEventReplay: () => void;
}

const SOURCE = "knowledge";

export const useKnowledgeStore = create<KnowledgeState>((set, get) => ({
  aspectsCache: new Map(),
  aspectDetailCache: new Map(),
  aspectsLoading: false,
  aspectDetailLoading: false,
  schemaCache: new Map(),
  schemaLoading: false,
  replayEntries: [],
  replayLoading: false,
  replayHasMore: false,
  replayNextCursor: 0,
  eventReplayEntries: [],
  eventReplayLoading: false,
  eventReplayHasMore: false,
  eventReplayNextCursor: null,
  columnSearchResults: [],
  columnSearchLoading: false,
  error: null,

  // =========================================================================
  // Actions — inline with error store integration (use get() for cache)
  // =========================================================================

  fetchAspects: async (urn, client) => {
    // Check cache
    if (get().aspectsCache.has(urn)) return;

    set({ aspectsLoading: true, error: null });
    try {
      const result = await client.get<{ aspects: string[] }>(
        `/api/v2/aspects/${encodeURIComponent(urn)}`,
      );
      const newCache = new Map(get().aspectsCache);
      newCache.set(urn, result.aspects ?? []);
      // Evict oldest entry if cache exceeds 100 URNs
      if (newCache.size > 100) {
        const oldest = newCache.keys().next().value;
        if (oldest !== undefined) newCache.delete(oldest);
      }
      set({ aspectsCache: newCache, aspectsLoading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch aspects";
      set({ aspectsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchAspectDetail: async (urn, name, client) => {
    const key = `${urn}::${name}`;
    if (get().aspectDetailCache.has(key)) return;

    set({ aspectDetailLoading: true, error: null });
    try {
      const result = await client.get<{
        aspectName: string;
        version: number;
        payload: Record<string, unknown>;
        createdBy: string;
      }>(
        `/api/v2/aspects/${encodeURIComponent(urn)}/${encodeURIComponent(name)}`,
      );
      const entry: AspectEntry = {
        name: result.aspectName ?? name,
        payload: result.payload ?? {},
        version: result.version ?? 0,
        createdBy: result.createdBy ?? "system",
      };
      const newCache = new Map(get().aspectDetailCache);
      newCache.set(key, entry);
      // Evict oldest entry if cache exceeds 50 aspect details
      if (newCache.size > 50) {
        const oldest = newCache.keys().next().value;
        if (oldest !== undefined) newCache.delete(oldest);
      }
      set({ aspectDetailCache: newCache, aspectDetailLoading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch aspect";
      set({ aspectDetailLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchSchema: async (path, client) => {
    const cacheKey = path;
    if (get().schemaCache.has(cacheKey)) return;

    set({ schemaLoading: true, error: null });
    try {
      const result = await client.get<{
        schema: {
          columns: SchemaColumn[];
          format: string;
          rowCount: number | null;
          confidence: number;
        } | null;
      }>(
        `/api/v2/catalog/schema/${encodeURIComponent(path.replace(/^\//, ""))}`,
      );
      const schema = result.schema
        ? {
            columns: result.schema.columns ?? [],
            format: result.schema.format ?? "unknown",
            rowCount: result.schema.rowCount ?? null,
            confidence: result.schema.confidence ?? 0,
          }
        : null;
      const newCache = new Map(get().schemaCache);
      newCache.set(cacheKey, schema);
      set({ schemaCache: newCache, schemaLoading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch schema";
      set({ schemaLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchReplay: async (
    client,
    fromSequence = 0,
    limit = 50,
    entityUrn?: string,
    aspectName?: string,
  ) => {
    set({ replayLoading: true, error: null });
    try {
      let url = `/api/v2/ops/replay?from_sequence=${fromSequence}&limit=${limit}`;
      if (entityUrn) url += `&entity_urn=${encodeURIComponent(entityUrn)}`;
      if (aspectName) url += `&aspect_name=${encodeURIComponent(aspectName)}`;
      const result = await client.get<{
        records: ReplayEntry[];
        nextCursor: number | null;
        hasMore: boolean;
      }>(url);
      const records: ReplayEntry[] = result.records ?? [];
      const entries = records.map((r) => ({
        sequenceNumber: r.sequenceNumber ?? 0,
        entityUrn: r.entityUrn ?? "",
        aspectName: r.aspectName ?? "",
        changeType: r.changeType ?? "",
        timestamp: r.timestamp ?? "",
      }));
      set({
        replayEntries:
          fromSequence === 0
            ? entries
            : [...get().replayEntries, ...entries],
        replayLoading: false,
        replayHasMore: result.hasMore ?? false,
        replayNextCursor: result.nextCursor ?? 0,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch replay";
      set({ replayLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchEventReplay: async (filters, client) => {
    set({ eventReplayLoading: true, error: null });
    try {
      const params = new URLSearchParams();
      if (filters.event_types) params.set("event_types", filters.event_types);
      if (filters.path_pattern) params.set("path_pattern", filters.path_pattern);
      if (filters.agent_id) params.set("agent_id", filters.agent_id);
      if (filters.since) params.set("since", filters.since);
      const cursor = get().eventReplayNextCursor;
      if (cursor) params.set("cursor", cursor);
      const qs = params.toString();
      const url = `/api/v2/events/replay${qs ? `?${qs}` : ""}`;
      const result = await client.get<{
        events: EventReplayEntry[];
        has_more: boolean;
        next_cursor: string | null;
      }>(url);
      const events: EventReplayEntry[] = (result.events ?? []).map((e) => ({
        event_id: e.event_id ?? "",
        event_type: e.event_type ?? "",
        agent_id: e.agent_id ?? null,
        path: e.path ?? null,
        timestamp: e.timestamp ?? "",
        payload: e.payload ?? {},
      }));
      set((state) => ({
        eventReplayEntries: cursor
          ? [...state.eventReplayEntries, ...events]
          : events,
        eventReplayLoading: false,
        eventReplayHasMore: result.has_more ?? false,
        eventReplayNextCursor: result.next_cursor ?? null,
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch event replay";
      set({ eventReplayLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  searchByColumn: createApiAction<KnowledgeState, [string, FetchClient]>(set, {
    loadingKey: "columnSearchLoading",
    source: SOURCE,
    errorMessage: "Failed to search",
    action: async (column, client) => {
      const result = await client.get<{
        results: {
          entityUrn: string;
          columnName: string;
          columnType: string;
        }[];
      }>(
        `/api/v2/catalog/search?column=${encodeURIComponent(column)}`,
      );
      return { columnSearchResults: result.results ?? [] };
    },
  }),

  clearReplay: () =>
    set({ replayEntries: [], replayNextCursor: 0, replayHasMore: false }),

  clearEventReplay: () =>
    set({ eventReplayEntries: [], eventReplayHasMore: false, eventReplayNextCursor: null }),
}));
