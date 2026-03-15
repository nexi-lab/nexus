/**
 * Zustand store for MCP (Model Context Protocol) server management.
 *
 * All operations use JSON-RPC via POST /api/nfs/{method}.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

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

export const useMcpStore = create<McpState>((set, get) => ({
  mounts: [],
  mountsLoading: false,
  selectedMountIndex: 0,
  tools: [],
  toolsLoading: false,
  error: null,

  fetchMounts: async (client) => {
    set({ mountsLoading: true, error: null });

    try {
      const response = await client.post<{
        result: readonly McpMount[];
      }>("/api/nfs/mcp_list_mounts", { params: {} });
      set({
        mounts: response.result ?? [],
        mountsLoading: false,
        selectedMountIndex: 0,
      });
    } catch (err) {
      set({
        mountsLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch MCP mounts",
      });
    }
  },

  mountServer: async (params, client) => {
    set({ error: null });

    try {
      await client.post("/api/nfs/mcp_mount", { params });
      await get().fetchMounts(client);
    } catch (err) {
      set({
        error:
          err instanceof Error ? err.message : "Failed to mount MCP server",
      });
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
      set({
        error:
          err instanceof Error
            ? err.message
            : "Failed to unmount MCP server",
      });
    }
  },

  syncServer: async (name, client) => {
    set({ error: null });

    try {
      await client.post("/api/nfs/mcp_sync", { params: { name } });
      await get().fetchMounts(client);
    } catch (err) {
      set({
        error:
          err instanceof Error ? err.message : "Failed to sync MCP server",
      });
    }
  },

  fetchTools: async (name, client) => {
    set({ toolsLoading: true, error: null });

    try {
      const response = await client.post<{
        result: readonly McpTool[];
      }>("/api/nfs/mcp_list_tools", { params: { name } });
      set({ tools: response.result ?? [], toolsLoading: false });
    } catch (err) {
      set({
        tools: [],
        toolsLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch MCP tools",
      });
    }
  },

  setSelectedMountIndex: (index) => set({ selectedMountIndex: index }),
}));
