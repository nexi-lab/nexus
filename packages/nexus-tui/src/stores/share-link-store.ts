/**
 * Store for share link management (via JSON-RPC).
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

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

export const useShareLinkStore = create<ShareLinkState>((set, get) => ({
  links: [],
  linksLoading: false,
  selectedLinkIndex: 0,
  accessLogs: [],
  accessLogsLoading: false,
  error: null,

  fetchLinks: async (client, options) => {
    set({ linksLoading: true, error: null });
    try {
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
      set({
        links: response.result ?? [],
        linksLoading: false,
        selectedLinkIndex: 0,
      });
    } catch (err) {
      set({
        linksLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch share links",
      });
    }
  },

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
      set({
        error: err instanceof Error ? err.message : "Failed to create share link",
      });
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
      set({
        error: err instanceof Error ? err.message : "Failed to revoke share link",
      });
    }
  },

  fetchAccessLogs: async (linkId, client) => {
    set({ accessLogsLoading: true, error: null });
    try {
      const response = await client.post<{ result: readonly ShareLinkAccessLog[] }>(
        "/api/nfs/get_share_link_access_logs",
        {
          params: { link_id: linkId },
        },
      );
      set({
        accessLogs: response.result ?? [],
        accessLogsLoading: false,
      });
    } catch (err) {
      set({
        accessLogsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch access logs",
      });
    }
  },

  setSelectedLinkIndex: (index) => {
    set({ selectedLinkIndex: index });
  },
}));
