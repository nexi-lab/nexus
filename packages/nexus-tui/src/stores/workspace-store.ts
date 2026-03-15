/**
 * Store for workspace and memory directory management.
 *
 * Backend prerequisite: #2987 (REST API). Until then, store actions
 * will call the expected endpoints and fail gracefully.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

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

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  workspaces: [],
  workspacesLoading: false,
  selectedWorkspaceIndex: 0,
  memories: [],
  memoriesLoading: false,
  selectedMemoryIndex: 0,
  error: null,

  fetchWorkspaces: async (client) => {
    set({ workspacesLoading: true, error: null });

    try {
      const response = await client.get<{
        workspaces: readonly WorkspaceInfo[];
      }>("/api/v2/workspaces");
      set({
        workspaces: response.workspaces ?? [],
        workspacesLoading: false,
        selectedWorkspaceIndex: 0,
      });
    } catch (err) {
      set({
        workspacesLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch workspaces",
      });
    }
  },

  registerWorkspace: async (params, client) => {
    set({ error: null });

    try {
      await client.post("/api/v2/workspaces", params);
      await get().fetchWorkspaces(client);
    } catch (err) {
      set({
        error:
          err instanceof Error
            ? err.message
            : "Failed to register workspace",
      });
    }
  },

  unregisterWorkspace: async (path, client) => {
    set({ error: null });

    try {
      await client.delete(
        `/api/v2/workspaces/${encodeURIComponent(path)}`,
      );
      set((state) => ({
        workspaces: state.workspaces.filter((w) => w.path !== path),
        selectedWorkspaceIndex: Math.min(
          state.selectedWorkspaceIndex,
          Math.max(state.workspaces.length - 2, 0),
        ),
      }));
    } catch (err) {
      set({
        error:
          err instanceof Error
            ? err.message
            : "Failed to unregister workspace",
      });
    }
  },

  fetchMemories: async (client) => {
    set({ memoriesLoading: true, error: null });

    try {
      const response = await client.get<{
        memories: readonly MemoryInfo[];
      }>("/api/v2/memories");
      set({
        memories: response.memories ?? [],
        memoriesLoading: false,
        selectedMemoryIndex: 0,
      });
    } catch (err) {
      set({
        memoriesLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch memories",
      });
    }
  },

  registerMemory: async (params, client) => {
    set({ error: null });

    try {
      await client.post("/api/v2/memories", params);
      await get().fetchMemories(client);
    } catch (err) {
      set({
        error:
          err instanceof Error ? err.message : "Failed to register memory",
      });
    }
  },

  unregisterMemory: async (path, client) => {
    set({ error: null });

    try {
      await client.delete(
        `/api/v2/memories/${encodeURIComponent(path)}`,
      );
      set((state) => ({
        memories: state.memories.filter((m) => m.path !== path),
        selectedMemoryIndex: Math.min(
          state.selectedMemoryIndex,
          Math.max(state.memories.length - 2, 0),
        ),
      }));
    } catch (err) {
      set({
        error:
          err instanceof Error
            ? err.message
            : "Failed to unregister memory",
      });
    }
  },

  setSelectedWorkspaceIndex: (index) => set({ selectedWorkspaceIndex: index }),
  setSelectedMemoryIndex: (index) => set({ selectedMemoryIndex: index }),
}));
