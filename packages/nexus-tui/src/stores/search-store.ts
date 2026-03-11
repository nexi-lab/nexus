/**
 * Zustand store for Search & Knowledge panel.
 *
 * Manages unified search, knowledge graph exploration,
 * agent memories, and playbooks.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

// =============================================================================
// Types (snake_case matching API wire format)
// =============================================================================

export interface SearchResult {
  readonly id: string;
  readonly type: string;
  readonly path: string | null;
  readonly title: string;
  readonly snippet: string;
  readonly score: number;
  readonly zone_id: string | null;
}

export interface KnowledgeEntity {
  readonly entity_id: string;
  readonly type: string;
  readonly name: string;
  readonly properties: Record<string, unknown>;
  readonly created_at: string;
  readonly updated_at: string;
}

export interface Memory {
  readonly memory_id: string;
  readonly agent_id: string;
  readonly type: string;
  readonly content: string;
  readonly tags: readonly string[];
  readonly version: number;
  readonly created_at: string;
  readonly updated_at: string;
}

export interface Playbook {
  readonly playbook_id: string;
  readonly name: string;
  readonly description: string;
  readonly steps: number;
  readonly last_run: string | null;
  readonly status: "active" | "draft" | "archived";
}

export type SearchTab = "search" | "knowledge" | "memories" | "playbooks";

interface SearchQueryResponse {
  readonly results: readonly SearchResult[];
  readonly total: number;
}

interface NeighborsResponse {
  readonly neighbors: readonly KnowledgeEntity[];
}

interface KnowledgeSearchResponse {
  readonly entities: readonly KnowledgeEntity[];
}

interface MemoryListResponse {
  readonly memories: readonly Memory[];
  readonly total: number;
}

interface PlaybookListResponse {
  readonly playbooks: readonly Playbook[];
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
  readonly entities: readonly KnowledgeEntity[];
  readonly selectedEntity: KnowledgeEntity | null;
  readonly neighbors: readonly KnowledgeEntity[];
  readonly knowledgeLoading: boolean;

  // Memories
  readonly memories: readonly Memory[];
  readonly selectedMemoryIndex: number;
  readonly memoriesLoading: boolean;

  // Playbooks
  readonly playbooks: readonly Playbook[];
  readonly playbooksLoading: boolean;

  // Shared
  readonly activeTab: SearchTab;
  readonly error: string | null;

  // Actions
  readonly search: (query: string, client: FetchClient) => Promise<void>;
  readonly fetchEntity: (id: string, client: FetchClient) => Promise<void>;
  readonly fetchNeighbors: (id: string, client: FetchClient) => Promise<void>;
  readonly searchKnowledge: (query: string, client: FetchClient) => Promise<void>;
  readonly fetchMemories: (client: FetchClient) => Promise<void>;
  readonly fetchMemoryDetail: (id: string, client: FetchClient) => Promise<void>;
  readonly fetchPlaybooks: (client: FetchClient) => Promise<void>;
  readonly setActiveTab: (tab: SearchTab) => void;
  readonly setSelectedResultIndex: (index: number) => void;
  readonly setSelectedMemoryIndex: (index: number) => void;
  readonly setSearchQuery: (query: string) => void;
}

export const useSearchStore = create<SearchState>((set) => ({
  searchQuery: "",
  searchResults: [],
  searchTotal: 0,
  selectedResultIndex: 0,
  searchLoading: false,

  entities: [],
  selectedEntity: null,
  neighbors: [],
  knowledgeLoading: false,

  memories: [],
  selectedMemoryIndex: 0,
  memoriesLoading: false,

  playbooks: [],
  playbooksLoading: false,

  activeTab: "search",
  error: null,

  search: async (query, client) => {
    set({ searchLoading: true, error: null, searchQuery: query });

    try {
      const response = await client.post<SearchQueryResponse>(
        "/api/v2/search/query",
        { query },
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
      const entity = await client.get<KnowledgeEntity>(
        `/api/v2/knowledge/entity/${encodeURIComponent(id)}`,
      );
      set({ selectedEntity: entity, knowledgeLoading: false });
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
        `/api/v2/knowledge/entity/${encodeURIComponent(id)}/neighbors`,
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

  searchKnowledge: async (query, client) => {
    set({ knowledgeLoading: true, error: null });

    try {
      const response = await client.post<KnowledgeSearchResponse>(
        "/api/v2/knowledge/search",
        { query },
      );
      set({
        entities: response.entities ?? [],
        knowledgeLoading: false,
      });
    } catch (err) {
      set({
        entities: [],
        knowledgeLoading: false,
        error: err instanceof Error ? err.message : "Failed to search knowledge graph",
      });
    }
  },

  fetchMemories: async (client) => {
    set({ memoriesLoading: true, error: null });

    try {
      const response = await client.get<MemoryListResponse>("/api/v2/memory");
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
      const memory = await client.get<Memory>(
        `/api/v2/memory/${encodeURIComponent(id)}`,
      );

      set((state) => {
        const updated = state.memories.map((m) =>
          m.memory_id === memory.memory_id ? memory : m,
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

  fetchPlaybooks: async (client) => {
    set({ playbooksLoading: true, error: null });

    try {
      const response = await client.get<PlaybookListResponse>("/api/v2/playbooks");
      set({
        playbooks: response.playbooks ?? [],
        playbooksLoading: false,
      });
    } catch (err) {
      set({
        playbooksLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch playbooks",
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
}));
