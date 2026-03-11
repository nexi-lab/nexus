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

export interface PlaybookRecord {
  readonly playbook_id: string;
  readonly name: string;
  readonly description: string;
  readonly scope: string;
  readonly tags: readonly string[];
  readonly steps: readonly unknown[];
  readonly metadata: Readonly<Record<string, unknown>> | null;
  readonly created_at: string;
  readonly updated_at: string;
  readonly usage_count: number;
  readonly success_rate: number;
}

export interface MemoryVersion {
  readonly version: number;
  readonly created_at: string;
  readonly status: string;
}

export interface MemoryHistory {
  readonly memory_id: string;
  readonly current_version: number;
  readonly versions: readonly MemoryVersion[];
}

export interface MemoryDiff {
  readonly diff: string;
  readonly mode: string;
  readonly v1: number;
  readonly v2: number;
}

export type SearchTab = "search" | "knowledge" | "memories" | "playbooks";
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

interface PlaybooksListResponse {
  readonly playbooks: readonly PlaybookRecord[];
  readonly total: number;
}

interface MemoryHistoryResponse {
  readonly memory_id: string;
  readonly current_version: number;
  readonly versions: readonly MemoryVersion[];
}

interface MemoryDiffResponse {
  readonly diff: string;
  readonly mode: string;
  readonly v1: number;
  readonly v2: number;
}

interface MemoryRollbackResponse {
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

  // Playbooks
  readonly playbooks: readonly PlaybookRecord[];
  readonly playbooksLoading: boolean;
  readonly selectedPlaybookIndex: number;

  // Memory versioning
  readonly memoryHistory: MemoryHistory | null;
  readonly memoryHistoryLoading: boolean;
  readonly memoryDiff: MemoryDiff | null;
  readonly memoryDiffLoading: boolean;

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
  readonly fetchPlaybooks: (query: string, client: FetchClient) => Promise<void>;
  readonly deletePlaybook: (id: string, client: FetchClient) => Promise<void>;
  readonly setSelectedPlaybookIndex: (index: number) => void;
  readonly fetchMemoryHistory: (memoryId: string, client: FetchClient) => Promise<void>;
  readonly fetchMemoryDiff: (memoryId: string, v1: number, v2: number, client: FetchClient) => Promise<void>;
  readonly rollbackMemory: (memoryId: string, version: number, client: FetchClient) => Promise<void>;
  readonly clearMemoryHistory: () => void;
  readonly clearMemoryDiff: () => void;
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

  playbooks: [],
  playbooksLoading: false,
  selectedPlaybookIndex: 0,

  memoryHistory: null,
  memoryHistoryLoading: false,
  memoryDiff: null,
  memoryDiffLoading: false,

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

  fetchPlaybooks: async (query, client) => {
    set({ playbooksLoading: true, error: null });

    try {
      const params = new URLSearchParams({ name_pattern: query });
      const response = await client.get<PlaybooksListResponse>(
        `/api/v2/playbooks?${params.toString()}`,
      );
      set({
        playbooks: response.playbooks ?? [],
        selectedPlaybookIndex: 0,
        playbooksLoading: false,
      });
    } catch (err) {
      set({
        playbooksLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch playbooks",
      });
    }
  },

  deletePlaybook: async (id, client) => {
    try {
      await client.delete(`/api/v2/playbooks/${encodeURIComponent(id)}`);
      set((state) => ({
        playbooks: state.playbooks.filter((p) => p.playbook_id !== id),
        selectedPlaybookIndex: Math.min(
          state.selectedPlaybookIndex,
          Math.max(state.playbooks.length - 2, 0),
        ),
        error: null,
      }));
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to delete playbook",
      });
    }
  },

  setSelectedPlaybookIndex: (index) => {
    set({ selectedPlaybookIndex: index });
  },

  fetchMemoryHistory: async (memoryId, client) => {
    set({ memoryHistoryLoading: true, error: null });

    try {
      const response = await client.get<MemoryHistoryResponse>(
        `/api/v2/memories/${encodeURIComponent(memoryId)}/history`,
      );
      set({
        memoryHistory: {
          memory_id: response.memory_id,
          current_version: response.current_version,
          versions: response.versions ?? [],
        },
        memoryHistoryLoading: false,
      });
    } catch (err) {
      set({
        memoryHistoryLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch memory history",
      });
    }
  },

  fetchMemoryDiff: async (memoryId, v1, v2, client) => {
    set({ memoryDiffLoading: true, error: null });

    try {
      const params = new URLSearchParams({
        v1: String(v1),
        v2: String(v2),
        mode: "content",
      });
      const response = await client.get<MemoryDiffResponse>(
        `/api/v2/memories/${encodeURIComponent(memoryId)}/diff?${params.toString()}`,
      );
      set({
        memoryDiff: {
          diff: response.diff,
          mode: response.mode,
          v1: response.v1,
          v2: response.v2,
        },
        memoryDiffLoading: false,
      });
    } catch (err) {
      set({
        memoryDiffLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch memory diff",
      });
    }
  },

  rollbackMemory: async (memoryId, version, client) => {
    set({ memoriesLoading: true, error: null });

    try {
      const response = await client.post<MemoryRollbackResponse>(
        `/api/v2/memories/${encodeURIComponent(memoryId)}/rollback`,
        { version },
      );
      const memory = response.memory;

      set((state) => {
        const updated = state.memories.map((m) =>
          (m as Record<string, unknown>).memory_id === memoryId ? memory : m,
        );
        return {
          memories: updated,
          memoriesLoading: false,
          memoryHistory: null,
          memoryDiff: null,
        };
      });
    } catch (err) {
      set({
        memoriesLoading: false,
        error: err instanceof Error ? err.message : "Failed to rollback memory",
      });
    }
  },

  clearMemoryHistory: () => {
    set({ memoryHistory: null });
  },

  clearMemoryDiff: () => {
    set({ memoryDiff: null });
  },
}));
