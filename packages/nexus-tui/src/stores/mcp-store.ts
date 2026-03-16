/**
 * Zustand store for MCP (Model Context Protocol) server management.
 *
 * All operations use JSON-RPC via POST /api/nfs/{method}.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";
import { createApiAction, categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";

// =============================================================================
// Types (snake_case matching API wire format)
// =============================================================================

export interface McpMount {
  readonly name: string;
  readonly description: string | null;
  readonly transport: "stdio" | "sse" | "klavis";
  readonly mounted: boolean;
  readonly tool_count: number;
  readonly last_sync: string | null;
}

export interface McpTool {
  readonly name: string;
  readonly description: string | null;
  readonly input_schema: unknown;
}

// =============================================================================
// Store
// =============================================================================

export interface McpState {
  readonly mounts: readonly McpMount[];
  readonly mountsLoading: boolean;
  readonly selectedMountIndex: number;
  readonly tools: readonly McpTool[];
  readonly toolsLoading: boolean;
  readonly error: string | null;

  readonly fetchMounts: (client: FetchClient) => Promise<void>;
  readonly mountServer: (
    params: {
      name: string;
      command?: string;
      url?: string;
      description?: string;
    },
    client: FetchClient,
  ) => Promise<void>;
  readonly unmountServer: (name: string, client: FetchClient) => Promise<void>;
  readonly syncServer: (name: string, client: FetchClient) => Promise<void>;
  readonly fetchTools: (name: string, client: FetchClient) => Promise<void>;
  readonly setSelectedMountIndex: (index: number) => void;
}

const SOURCE = "mcp";

export const useMcpStore = create<McpState>((set, get) => ({
  mounts: [],
  mountsLoading: false,
  selectedMountIndex: 0,
  tools: [],
  toolsLoading: false,
  error: null,

  // =========================================================================
  // Actions with loading keys — createApiAction
  // =========================================================================

  fetchMounts: createApiAction<McpState, [FetchClient]>(set, {
    loadingKey: "mountsLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch MCP mounts",
    action: async (client) => {
      const response = await client.post<{
        result: readonly McpMount[];
      }>("/api/nfs/mcp_list_mounts", { params: {} });
      return {
        mounts: response.result ?? [],
        selectedMountIndex: 0,
      };
    },
  }),

  fetchTools: createApiAction<McpState, [string, FetchClient]>(set, {
    loadingKey: "toolsLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch MCP tools",
    action: async (name, client) => {
      const response = await client.post<{
        result: readonly McpTool[];
      }>("/api/nfs/mcp_list_tools", { params: { name } });
      return { tools: response.result ?? [] };
    },
  }),

  // =========================================================================
  // Actions without loading keys — inline with error store integration
  // =========================================================================

  mountServer: async (params, client) => {
    set({ error: null });
    try {
      await client.post("/api/nfs/mcp_mount", { params });
      await get().fetchMounts(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to mount MCP server";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  unmountServer: async (name, client) => {
    set({ error: null });
    try {
      await client.post("/api/nfs/mcp_unmount", { params: { name } });
      set((state) => ({
        mounts: state.mounts.filter((m) => m.name !== name),
        selectedMountIndex: Math.min(
          state.selectedMountIndex,
          Math.max(state.mounts.length - 2, 0),
        ),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to unmount MCP server";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  syncServer: async (name, client) => {
    set({ error: null });
    try {
      await client.post("/api/nfs/mcp_sync", { params: { name } });
      await get().fetchMounts(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to sync MCP server";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  setSelectedMountIndex: (index) => set({ selectedMountIndex: index }),
}));
