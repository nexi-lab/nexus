/**
 * Zustand store for Search & Knowledge panel.
 *
 * Manages unified search, knowledge graph exploration,
 * and agent memories.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";
import { createApiAction, categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";

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

/** Matches backend PlaybookResponse from playbook.py. */
export interface PlaybookRecord {
  readonly playbook_id: string;
  readonly name: string;
  readonly description: string | null;
  readonly version: number;
  readonly scope: string;
  readonly visibility: string;
  readonly usage_count: number;
  readonly success_rate: number | null;
  readonly strategies: readonly unknown[] | null;
  readonly created_at: string | null;
  readonly updated_at: string | null;
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

/** A single RLM inference iteration (rlm.iteration SSE event). */
export interface RlmStep {
  readonly step: number;
  readonly code_executed: string;
  readonly output_summary: string;
  readonly tokens_used: number;
  readonly duration_seconds: number;
}

/** Progressive state of an RLM streaming inference. */
export interface RlmAnswer {
  readonly status: "streaming" | "completed" | "budget_exceeded" | "error";
  readonly answer: string | null;
  readonly total_tokens: number;
  readonly total_duration_seconds: number;
  readonly iterations: number;
  readonly error_message: string | null;
  readonly steps: readonly RlmStep[];
  readonly model: string | null;
}

export type SearchTab = "search" | "knowledge" | "memories" | "playbooks" | "ask" | "columns";
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

/** Matches backend rollback response from memories.py:480. */
interface MemoryRollbackResponse {
  readonly rolled_back: boolean;
  readonly memory_id: string;
  readonly rolled_back_to_version: number;
  readonly current_version: number | null;
  readonly content: unknown;
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

  // RLM Q&A (document-scoped via context_paths)
  readonly rlmAnswer: RlmAnswer | null;
  readonly rlmLoading: boolean;
  readonly rlmContextPaths: readonly string[];

  // Search mode
  readonly searchMode: SearchMode;

  // Input buffer (persists across tab switches)
  readonly inputBuffer: string;

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
  readonly setInputBuffer: (buffer: string) => void;
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
  readonly addRlmContextPath: (path: string) => void;
  readonly removeRlmContextPath: (path: string) => void;
  readonly clearRlmContextPaths: () => void;
  readonly askRlm: (query: string, client: FetchClient, zoneId?: string) => Promise<void>;

  // Memory mutations
  readonly createMemory: (content: string, metadata: Record<string, unknown>, client: FetchClient) => Promise<void>;
  readonly updateMemory: (memoryId: string, content: string, client: FetchClient) => Promise<void>;
  readonly deleteMemory: (memoryId: string, client: FetchClient) => Promise<void>;
}

const SOURCE = "search";

let activeAskController: AbortController | null = null;

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

  rlmAnswer: null,
  rlmLoading: false,
  rlmContextPaths: [],

  inputBuffer: "",

  searchMode: "hybrid",

  activeTab: "search",
  error: null,

  // =========================================================================
  // Actions migrated to createApiAction (Decision 6A)
  // =========================================================================

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
      const message = err instanceof Error ? err.message : "Failed to search";
      set({ searchLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchEntity: createApiAction<SearchState, [string, FetchClient]>(set, {
    loadingKey: "knowledgeLoading",
    source: SOURCE,
    action: async (id, client) => {
      const response = await client.get<EntityResponse>(
        `/api/v2/graph/entity/${encodeURIComponent(id)}`,
      );
      return { selectedEntity: response.entity ?? null };
    },
  }),

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
      const message = err instanceof Error ? err.message : "Failed to fetch neighbors";
      set({
        neighbors: [],
        knowledgeLoading: false,
        error: message,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
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
      const message = err instanceof Error ? err.message : "Failed to search knowledge graph";
      set({
        knowledgeSearchResult: null,
        knowledgeLoading: false,
        error: message,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchMemories: createApiAction<SearchState, [string, FetchClient]>(set, {
    loadingKey: "memoriesLoading",
    source: SOURCE,
    action: async (query, client) => {
      const response = await client.post<MemorySearchResponse>(
        "/api/v2/memories/search",
        { query },
      );
      return {
        memories: response.memories ?? [],
        selectedMemoryIndex: 0,
      };
    },
  }),

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
      const message = err instanceof Error ? err.message : "Failed to fetch memory detail";
      set({ memoriesLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
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

  setInputBuffer: (buffer) => {
    set({ inputBuffer: buffer });
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

  fetchPlaybooks: createApiAction<SearchState, [string, FetchClient]>(set, {
    loadingKey: "playbooksLoading",
    source: SOURCE,
    action: async (query, client) => {
      const params = new URLSearchParams({ name_pattern: query });
      const response = await client.get<PlaybooksListResponse>(
        `/api/v2/playbooks?${params.toString()}`,
      );
      return {
        playbooks: response.playbooks ?? [],
        selectedPlaybookIndex: 0,
      };
    },
  }),

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
      const message = err instanceof Error ? err.message : "Failed to delete playbook";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  setSelectedPlaybookIndex: (index) => {
    set({ selectedPlaybookIndex: index });
  },

  fetchMemoryHistory: createApiAction<SearchState, [string, FetchClient]>(set, {
    loadingKey: "memoryHistoryLoading",
    source: SOURCE,
    action: async (memoryId, client) => {
      const response = await client.get<MemoryHistoryResponse>(
        `/api/v2/memories/${encodeURIComponent(memoryId)}/history`,
      );
      return {
        memoryHistory: {
          memory_id: response.memory_id,
          current_version: response.current_version,
          versions: response.versions ?? [],
        },
      };
    },
  }),

  fetchMemoryDiff: createApiAction<SearchState, [string, number, number, FetchClient]>(set, {
    loadingKey: "memoryDiffLoading",
    source: SOURCE,
    action: async (memoryId, v1, v2, client) => {
      const params = new URLSearchParams({
        v1: String(v1),
        v2: String(v2),
        mode: "content",
      });
      const response = await client.get<MemoryDiffResponse>(
        `/api/v2/memories/${encodeURIComponent(memoryId)}/diff?${params.toString()}`,
      );
      return {
        memoryDiff: {
          diff: response.diff,
          mode: response.mode,
          v1: response.v1,
          v2: response.v2,
        },
      };
    },
  }),

  rollbackMemory: async (memoryId, version, client) => {
    set({ memoriesLoading: true, error: null });

    try {
      // Backend takes version as query param (memories.py:480), not body
      await client.post<MemoryRollbackResponse>(
        `/api/v2/memories/${encodeURIComponent(memoryId)}/rollback?version=${version}`,
        {},
      );

      // Clear versioning state and refresh memories list
      set({
        memoriesLoading: false,
        memoryHistory: null,
        memoryDiff: null,
      });

      // Re-fetch memories to get updated state
      const query = get().searchQuery;
      if (query) {
        await get().fetchMemories(query, client);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to rollback memory";
      set({ memoriesLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  clearMemoryHistory: () => {
    set({ memoryHistory: null });
  },

  clearMemoryDiff: () => {
    set({ memoryDiff: null });
  },

  addRlmContextPath: (path) => {
    set((state) => {
      if (state.rlmContextPaths.includes(path)) return state;
      return { rlmContextPaths: [...state.rlmContextPaths, path] };
    });
  },

  removeRlmContextPath: (path) => {
    set((state) => ({
      rlmContextPaths: state.rlmContextPaths.filter((p) => p !== path),
    }));
  },

  clearRlmContextPaths: () => {
    set({ rlmContextPaths: [] });
  },

  // =========================================================================
  // SSE streaming — left as-is with error store integration
  // =========================================================================

  askRlm: async (query, client, zoneId) => {
    // Cancel previous request
    activeAskController?.abort();
    activeAskController = new AbortController();
    const signal = activeAskController.signal;

    const initial: RlmAnswer = {
      status: "streaming",
      answer: null,
      total_tokens: 0,
      total_duration_seconds: 0,
      iterations: 0,
      error_message: null,
      steps: [],
      model: null,
    };
    set({ rlmLoading: true, error: null, rlmAnswer: initial });

    try {
      const { rlmContextPaths } = get();
      const body: Record<string, unknown> = { query, stream: true };
      if (zoneId) {
        body.zone_id = zoneId;
      }
      if (rlmContextPaths.length > 0) {
        body.context_paths = rlmContextPaths;
      }

      const response = await client.rawRequest(
        "POST",
        "/api/v2/rlm/infer",
        JSON.stringify(body),
      );

      if (!response.ok) {
        const errText = await response.text();
        set({
          rlmLoading: false,
          rlmAnswer: { ...initial, status: "error", error_message: `HTTP ${response.status}: ${errText}` },
        });
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) {
        set({ rlmLoading: false, error: "No response body from RLM" });
        return;
      }

      const decoder = new TextDecoder();
      let buffer = "";

      for (;;) {
        if (signal.aborted) break;
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() ?? "";

        for (const part of parts) {
          if (signal.aborted) break;
          let eventName = "";
          let dataStr = "";
          for (const line of part.split("\n")) {
            if (line.startsWith("event: ")) {
              eventName = line.slice(7).trim();
            } else if (line.startsWith("data: ")) {
              dataStr += line.slice(6);
            }
          }
          if (!eventName || !dataStr) continue;

          try {
            const data = JSON.parse(dataStr) as Record<string, unknown>;
            if (signal.aborted) break;
            const current = get().rlmAnswer ?? initial;

            if (eventName === "rlm.started") {
              set({
                rlmAnswer: { ...current, model: (data.model as string) ?? null },
              });
            } else if (eventName === "rlm.iteration") {
              const step: RlmStep = {
                step: data.step as number,
                code_executed: data.code_executed as string,
                output_summary: data.output_summary as string,
                tokens_used: data.tokens_used as number,
                duration_seconds: data.duration_seconds as number,
              };
              set({
                rlmAnswer: {
                  ...current,
                  steps: [...current.steps, step],
                  iterations: data.step as number,
                  total_tokens: current.total_tokens + (data.tokens_used as number),
                },
              });
            } else if (eventName === "rlm.final_answer") {
              set({
                rlmAnswer: {
                  ...current,
                  status: "completed",
                  answer: data.answer as string,
                  total_tokens: data.total_tokens as number,
                  total_duration_seconds: data.total_duration_seconds as number,
                  iterations: data.iterations as number,
                },
                rlmLoading: false,
              });
            } else if (eventName === "rlm.budget_exceeded") {
              set({
                rlmAnswer: {
                  ...current,
                  status: "budget_exceeded",
                  error_message: data.reason as string,
                  total_tokens: data.total_tokens as number,
                  iterations: data.iterations as number,
                },
                rlmLoading: false,
              });
            } else if (eventName === "rlm.error") {
              set({
                rlmAnswer: {
                  ...current,
                  status: "error",
                  error_message: data.error as string,
                },
                rlmLoading: false,
              });
            }
          } catch {
            // Skip malformed SSE events
          }
        }
      }

      // If stream ended without a terminal event, mark complete
      if (!signal.aborted && get().rlmLoading) {
        set({ rlmLoading: false });
      }
    } catch (err) {
      if (signal.aborted) return; // Request was cancelled; ignore error
      const message = err instanceof Error ? err.message : "Failed to query RLM";
      set({ rlmLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  // =========================================================================
  // Memory mutations — inline with error store integration
  // =========================================================================

  createMemory: async (content, metadata, client) => {
    set({ memoriesLoading: true, error: null });
    try {
      await client.post("/api/v2/memories", { content, metadata });
      // Re-fetch to get updated list
      const query = get().searchQuery;
      await get().fetchMemories(query || "", client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to create memory";
      set({ memoriesLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  updateMemory: async (memoryId, content, client) => {
    set({ memoriesLoading: true, error: null });
    try {
      await client.put(`/api/v2/memories/${encodeURIComponent(memoryId)}`, { content });
      set({ memoriesLoading: false });
      // Re-fetch detail
      await get().fetchMemoryDetail(memoryId, client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to update memory";
      set({ memoriesLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  deleteMemory: async (memoryId, client) => {
    set({ memoriesLoading: true, error: null });
    try {
      await client.delete(`/api/v2/memories/${encodeURIComponent(memoryId)}`);
      set((state) => ({
        memories: state.memories.filter((m) => (m as Record<string, unknown>).memory_id !== memoryId),
        selectedMemoryIndex: Math.min(state.selectedMemoryIndex, Math.max(state.memories.length - 2, 0)),
        memoriesLoading: false,
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to delete memory";
      set({ memoriesLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },
}));
