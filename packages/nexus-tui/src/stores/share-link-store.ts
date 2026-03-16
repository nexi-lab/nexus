/**
 * Store for share link management (via JSON-RPC).
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";
import { createApiAction, categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";

// =============================================================================
// Types
// =============================================================================

export interface ShareLink {
  readonly link_id: string;
  readonly path: string;
  readonly permission_level: string;
  readonly status: string;
  readonly access_count: number;
  readonly has_password: boolean;
  readonly expires_at: string | null;
  readonly created_at: string;
}

export interface ShareLinkAccessLog {
  readonly timestamp: string;
  readonly ip_address: string;
  readonly user_agent: string;
  readonly success: boolean;
}

// =============================================================================
// Store
// =============================================================================

export interface ShareLinkState {
  readonly links: readonly ShareLink[];
  readonly linksLoading: boolean;
  readonly selectedLinkIndex: number;
  readonly accessLogs: readonly ShareLinkAccessLog[];
  readonly accessLogsLoading: boolean;
  readonly error: string | null;

  readonly fetchLinks: (client: FetchClient, options?: { includeRevoked?: boolean; includeExpired?: boolean }) => Promise<void>;
  readonly createLink: (params: { path: string; permission_level: string; password?: string; expires_in_hours?: number; max_access_count?: number }, client: FetchClient) => Promise<void>;
  readonly revokeLink: (linkId: string, client: FetchClient) => Promise<void>;
  readonly fetchAccessLogs: (linkId: string, client: FetchClient) => Promise<void>;
  readonly setSelectedLinkIndex: (index: number) => void;
}

const SOURCE = "files";

export const useShareLinkStore = create<ShareLinkState>((set, get) => ({
  links: [],
  linksLoading: false,
  selectedLinkIndex: 0,
  accessLogs: [],
  accessLogsLoading: false,
  error: null,

  // =========================================================================
  // Actions with loading keys — createApiAction
  // =========================================================================

  fetchLinks: createApiAction<ShareLinkState, [FetchClient, ({ includeRevoked?: boolean; includeExpired?: boolean } | undefined)?]>(set, {
    loadingKey: "linksLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch share links",
    action: async (client, options) => {
      const response = await client.post<{ result: readonly ShareLink[] }>(
        "/api/nfs/list_share_links",
        {
          params: {
            path: "",
            include_revoked: options?.includeRevoked ?? false,
            include_expired: options?.includeExpired ?? false,
          },
        },
      );
      return {
        links: response.result ?? [],
        selectedLinkIndex: 0,
      };
    },
  }),

  fetchAccessLogs: createApiAction<ShareLinkState, [string, FetchClient]>(set, {
    loadingKey: "accessLogsLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch access logs",
    action: async (linkId, client) => {
      const response = await client.post<{ result: readonly ShareLinkAccessLog[] }>(
        "/api/nfs/get_share_link_access_logs",
        {
          params: { link_id: linkId },
        },
      );
      return {
        accessLogs: response.result ?? [],
      };
    },
  }),

  // =========================================================================
  // Actions without loading keys — inline with error store integration
  // =========================================================================

  createLink: async (params, client) => {
    set({ error: null });
    try {
      await client.post<{ result: ShareLink }>(
        "/api/nfs/create_share_link",
        {
          params: {
            path: params.path,
            permission_level: params.permission_level,
            password: params.password,
            expires_in_hours: params.expires_in_hours,
            max_access_count: params.max_access_count,
          },
        },
      );
      await get().fetchLinks(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to create share link";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  revokeLink: async (linkId, client) => {
    set({ error: null });
    try {
      await client.post<{ result: unknown }>(
        "/api/nfs/revoke_share_link",
        {
          params: { link_id: linkId },
        },
      );
      await get().fetchLinks(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to revoke share link";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  setSelectedLinkIndex: (index) => {
    set({ selectedLinkIndex: index });
  },
}));
