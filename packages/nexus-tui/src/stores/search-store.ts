/**
 * Zustand store for Search & Knowledge panel.
 *
 * Manages unified search, knowledge graph exploration,
 * and agent memories.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

// =============================================================================
// Types (snake_case matching API wire format)
// =============================================================================

export interface SearchResult {
  readonly path: string;
  readonly chunk_text: string;
  readonly score: number;
  readonly chunk_index: number;
  readonly line_start: number;
  readonly line_end: number;
  readonly keyword_score: number | null;
  readonly vector_score: number | null;
}

export interface KnowledgeEntity {
  readonly [key: string]: unknown;
}

export interface NeighborEntry {
  readonly entity: KnowledgeEntity;
  readonly depth: number;
  readonly path: readonly string[];
}

export interface Memory {
  readonly [key: string]: unknown;
}

export type SearchTab = "search" | "knowledge" | "memories";
export type SearchMode = "keyword" | "semantic" | "hybrid";

const SEARCH_MODE_ORDER: readonly SearchMode[] = ["keyword", "semantic", "hybrid"];

interface SearchQueryResponse {
  readonly query: string;
  readonly search_type: string;
  readonly graph_mode: string;
  readonly results: readonly SearchResult[];
  readonly total: number;
  readonly latency_ms: number;
}

interface EntityResponse {
  readonly entity: KnowledgeEntity | null;
}

interface NeighborsResponse {
  readonly neighbors: readonly NeighborEntry[];
}

interface KnowledgeSearchResponse {
  readonly entity: KnowledgeEntity | null;
}

interface MemorySearchResponse {
  readonly memories: readonly Memory[];
}

interface MemoryDetailResponse {
  readonly memory: Memory;
}

// =============================================================================
// Store
// =============================================================================

export interface SearchState {
  // Search
  readonly searchQuery: string;
  readonly searchResults: readonly SearchResult[];
  readonly searchTotal: number;
  readonly selectedResultIndex: number;
  readonly searchLoading: boolean;

  // Knowledge graph
  readonly selectedEntity: KnowledgeEntity | null;
  readonly neighbors: readonly NeighborEntry[];
  readonly knowledgeSearchResult: KnowledgeEntity | null;
  readonly knowledgeLoading: boolean;

  // Memories
  readonly memories: readonly Memory[];
  readonly selectedMemoryIndex: number;
  readonly memoriesLoading: boolean;

  // Search mode
  readonly searchMode: SearchMode;

  // Shared
  readonly activeTab: SearchTab;
  readonly error: string | null;

  // Actions
  readonly search: (query: string, client: FetchClient) => Promise<void>;
  readonly fetchEntity: (id: string, client: FetchClient) => Promise<void>;
  readonly fetchNeighbors: (id: string, client: FetchClient) => Promise<void>;
  readonly searchKnowledge: (name: string, client: FetchClient) => Promise<void>;
  readonly fetchMemories: (query: string, client: FetchClient) => Promise<void>;
  readonly fetchMemoryDetail: (id: string, client: FetchClient) => Promise<void>;
  readonly setActiveTab: (tab: SearchTab) => void;
  readonly setSelectedResultIndex: (index: number) => void;
  readonly setSelectedMemoryIndex: (index: number) => void;
  readonly setSearchQuery: (query: string) => void;
  readonly setSearchMode: (mode: SearchMode) => void;
  readonly cycleSearchMode: () => void;
}

export const useSearchStore = create<SearchState>((set, get) => ({
  searchQuery: "",
  searchResults: [],
  searchTotal: 0,
  selectedResultIndex: 0,
  searchLoading: false,

  selectedEntity: null,
  neighbors: [],
  knowledgeSearchResult: null,
  knowledgeLoading: false,

  memories: [],
  selectedMemoryIndex: 0,
  memoriesLoading: false,

  searchMode: "hybrid",

  activeTab: "search",
  error: null,

  search: async (query, client) => {
    set({ searchLoading: true, error: null, searchQuery: query });

    try {
      const { searchMode } = get();
      const params = new URLSearchParams({
        q: query,
        type: searchMode,
        limit: "10",
      });
      const response = await client.get<SearchQueryResponse>(
        `/api/v2/search/query?${params.toString()}`,
      );

      const results = response.results ?? [];
      set({
        searchResults: results,
        searchTotal: response.total ?? results.length,
        selectedResultIndex: 0,
        searchLoading: false,
      });
    } catch (err) {
      set({
        searchLoading: false,
        error: err instanceof Error ? err.message : "Failed to search",
      });
    }
  },

  fetchEntity: async (id, client) => {
    set({ knowledgeLoading: true, error: null });

    try {
      const response = await client.get<EntityResponse>(
        `/api/v2/graph/entity/${encodeURIComponent(id)}`,
      );
      set({ selectedEntity: response.entity ?? null, knowledgeLoading: false });
    } catch (err) {
      set({
        knowledgeLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch entity",
      });
    }
  },

  fetchNeighbors: async (id, client) => {
    set({ knowledgeLoading: true, error: null });

    try {
      const response = await client.get<NeighborsResponse>(
        `/api/v2/graph/entity/${encodeURIComponent(id)}/neighbors?hops=1&direction=both`,
      );
      set({
        neighbors: response.neighbors ?? [],
        knowledgeLoading: false,
      });
    } catch (err) {
      set({
        neighbors: [],
        knowledgeLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch neighbors",
      });
    }
  },

  searchKnowledge: async (name, client) => {
    set({ knowledgeLoading: true, error: null });

    try {
      const params = new URLSearchParams({ name, fuzzy: "false" });
      const response = await client.get<KnowledgeSearchResponse>(
        `/api/v2/graph/search?${params.toString()}`,
      );
      set({
        knowledgeSearchResult: response.entity ?? null,
        knowledgeLoading: false,
      });
    } catch (err) {
      set({
        knowledgeSearchResult: null,
        knowledgeLoading: false,
        error: err instanceof Error ? err.message : "Failed to search knowledge graph",
      });
    }
  },

  fetchMemories: async (query, client) => {
    set({ memoriesLoading: true, error: null });

    try {
      const response = await client.post<MemorySearchResponse>(
        "/api/v2/memories/search",
        { query },
      );
      set({
        memories: response.memories ?? [],
        selectedMemoryIndex: 0,
        memoriesLoading: false,
      });
    } catch (err) {
      set({
        memoriesLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch memories",
      });
    }
  },

  fetchMemoryDetail: async (id, client) => {
    set({ memoriesLoading: true, error: null });

    try {
      const response = await client.get<MemoryDetailResponse>(
        `/api/v2/memories/${encodeURIComponent(id)}`,
      );
      const memory = response.memory;

      set((state) => {
        const memoryId = (memory as Record<string, unknown>).memory_id;
        const updated = state.memories.map((m) =>
          (m as Record<string, unknown>).memory_id === memoryId ? memory : m,
        );
        return { memories: updated, memoriesLoading: false };
      });
    } catch (err) {
      set({
        memoriesLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch memory detail",
      });
    }
  },

  setActiveTab: (tab) => {
    set({ activeTab: tab, error: null });
  },

  setSelectedResultIndex: (index) => {
    set({ selectedResultIndex: index });
  },

  setSelectedMemoryIndex: (index) => {
    set({ selectedMemoryIndex: index });
  },

  setSearchQuery: (query) => {
    set({ searchQuery: query });
  },

  setSearchMode: (mode) => {
    set({ searchMode: mode });
  },

  cycleSearchMode: () => {
    const { searchMode } = get();
    const currentIdx = SEARCH_MODE_ORDER.indexOf(searchMode);
    const nextIdx = (currentIdx + 1) % SEARCH_MODE_ORDER.length;
    const nextMode = SEARCH_MODE_ORDER[nextIdx];
    if (nextMode) {
      set({ searchMode: nextMode });
    }
  },
}));
