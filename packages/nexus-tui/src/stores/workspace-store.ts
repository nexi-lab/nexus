/**
 * Store for workspace and memory directory management.
 *
 * Workspaces use REST API at /api/v2/registry/workspaces (#2987, merged).
 * Memory directories use JSON-RPC via /api/nfs/ (register_workspace with
 * memory-specific params).
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";
import { createApiAction, categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";

// =============================================================================
// Types (snake_case matching API wire format)
// =============================================================================

export interface WorkspaceInfo {
  readonly path: string;
  readonly name: string;
  readonly description: string | null;
  readonly scope: "persistent" | "session";
  readonly ttl_seconds: number | null;
  readonly created_by: string | null;
  readonly created_at: string | null;
}

export interface MemoryInfo {
  readonly path: string;
  readonly name: string;
  readonly description: string | null;
  readonly scope: "persistent" | "session";
  readonly created_by: string | null;
  readonly created_at: string | null;
}

// =============================================================================
// Store
// =============================================================================

export interface WorkspaceState {
  readonly workspaces: readonly WorkspaceInfo[];
  readonly workspacesLoading: boolean;
  readonly selectedWorkspaceIndex: number;
  readonly memories: readonly MemoryInfo[];
  readonly memoriesLoading: boolean;
  readonly selectedMemoryIndex: number;
  readonly error: string | null;

  readonly fetchWorkspaces: (client: FetchClient) => Promise<void>;
  readonly registerWorkspace: (
    params: {
      path: string;
      name: string;
      description?: string;
      scope?: string;
      ttl_seconds?: number;
    },
    client: FetchClient,
  ) => Promise<void>;
  readonly unregisterWorkspace: (path: string, client: FetchClient) => Promise<void>;
  readonly fetchMemories: (client: FetchClient) => Promise<void>;
  readonly registerMemory: (
    params: { path: string; name: string; description?: string },
    client: FetchClient,
  ) => Promise<void>;
  readonly unregisterMemory: (path: string, client: FetchClient) => Promise<void>;
  readonly setSelectedWorkspaceIndex: (index: number) => void;
  readonly setSelectedMemoryIndex: (index: number) => void;
}

const SOURCE = "workspaces";

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  workspaces: [],
  workspacesLoading: false,
  selectedWorkspaceIndex: 0,
  memories: [],
  memoriesLoading: false,
  selectedMemoryIndex: 0,
  error: null,

  // =========================================================================
  // Actions with loading keys — createApiAction
  // =========================================================================

  fetchWorkspaces: createApiAction<WorkspaceState, [FetchClient]>(set, {
    loadingKey: "workspacesLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch workspaces",
    action: async (client) => {
      const response = await client.get<{
        workspaces: readonly WorkspaceInfo[];
      }>("/api/v2/registry/workspaces");
      return {
        workspaces: response.workspaces ?? [],
        selectedWorkspaceIndex: 0,
      };
    },
  }),

  fetchMemories: createApiAction<WorkspaceState, [FetchClient]>(set, {
    loadingKey: "memoriesLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch memories",
    action: async (client) => {
      const response = await client.post<{
        result: { memories: readonly MemoryInfo[] };
      }>("/api/nfs/list_workspaces", { params: { type: "memory" } });
      return {
        memories: response.result?.memories ?? [],
        selectedMemoryIndex: 0,
      };
    },
  }),

  // =========================================================================
  // Actions without loading keys — inline with error store integration
  // =========================================================================

  registerWorkspace: async (params, client) => {
    set({ error: null });
    try {
      await client.post("/api/v2/registry/workspaces", params);
      await get().fetchWorkspaces(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to register workspace";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  unregisterWorkspace: async (path, client) => {
    set({ error: null });
    try {
      await client.delete(
        `/api/v2/registry/workspaces/${encodeURIComponent(path)}`,
      );
      set((state) => ({
        workspaces: state.workspaces.filter((w) => w.path !== path),
        selectedWorkspaceIndex: Math.min(
          state.selectedWorkspaceIndex,
          Math.max(state.workspaces.length - 2, 0),
        ),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to unregister workspace";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  registerMemory: async (params, client) => {
    set({ error: null });
    try {
      await client.post("/api/nfs/register_workspace", { params: { ...params, type: "memory" } });
      await get().fetchMemories(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to register memory";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  unregisterMemory: async (path, client) => {
    set({ error: null });
    try {
      await client.post("/api/nfs/unregister_workspace", { params: { path } });
      set((state) => ({
        memories: state.memories.filter((m) => m.path !== path),
        selectedMemoryIndex: Math.min(
          state.selectedMemoryIndex,
          Math.max(state.memories.length - 2, 0),
        ),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to unregister memory";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  setSelectedWorkspaceIndex: (index) => set({ selectedWorkspaceIndex: index }),
  setSelectedMemoryIndex: (index) => set({ selectedMemoryIndex: index }),
}));
