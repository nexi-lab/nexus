/**
 * Zustand store for knowledge platform data: aspects, schemas, MCL replay.
 * Issue #2930.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

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

// =============================================================================
// Store
// =============================================================================

export interface KnowledgeState {
  // Aspects cache (keyed by URN)
  readonly aspectsCache: ReadonlyMap<string, readonly string[]>;
  readonly aspectDetailCache: ReadonlyMap<string, AspectEntry>;
  readonly aspectsLoading: boolean;

  // Schema cache (keyed by URN)
  readonly schemaCache: ReadonlyMap<string, SchemaInfo | null>;
  readonly schemaLoading: boolean;

  // MCL replay
  readonly replayEntries: readonly ReplayEntry[];
  readonly replayLoading: boolean;
  readonly replayHasMore: boolean;
  readonly replayNextCursor: number;

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
  readonly searchByColumn: (
    column: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly clearReplay: () => void;
}

export const useKnowledgeStore = create<KnowledgeState>((set, get) => ({
  aspectsCache: new Map(),
  aspectDetailCache: new Map(),
  aspectsLoading: false,
  schemaCache: new Map(),
  schemaLoading: false,
  replayEntries: [],
  replayLoading: false,
  replayHasMore: false,
  replayNextCursor: 0,
  columnSearchResults: [],
  columnSearchLoading: false,
  error: null,

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
      set({ aspectsCache: newCache, aspectsLoading: false });
    } catch (err) {
      set({
        aspectsLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch aspects",
      });
    }
  },

  fetchAspectDetail: async (urn, name, client) => {
    const key = `${urn}::${name}`;
    if (get().aspectDetailCache.has(key)) return;

    set({ aspectsLoading: true, error: null });
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
      set({ aspectDetailCache: newCache, aspectsLoading: false });
    } catch (err) {
      set({
        aspectsLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch aspect",
      });
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
      set({
        schemaLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch schema",
      });
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
      set({
        replayLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch replay",
      });
    }
  },

  searchByColumn: async (column, client) => {
    set({ columnSearchLoading: true, error: null });
    try {
      const result = await client.get<{
        results: {
          entityUrn: string;
          columnName: string;
          columnType: string;
        }[];
      }>(
        `/api/v2/catalog/search?column=${encodeURIComponent(column)}`,
      );
      set({
        columnSearchResults: result.results ?? [],
        columnSearchLoading: false,
      });
    } catch (err) {
      set({
        columnSearchLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to search",
      });
    }
  },

  clearReplay: () =>
    set({ replayEntries: [], replayNextCursor: 0, replayHasMore: false }),
}));
