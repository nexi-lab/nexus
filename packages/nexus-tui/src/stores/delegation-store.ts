/**
 * Shared delegation store (Decision 5A).
 *
 * Extracted from access-store.ts and agents-store.ts to eliminate duplication.
 * Both the Access panel and Agents panel consume from this single store.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";
import { createApiAction, categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";

// Canonical DelegationItem type — other stores re-export this.
export interface DelegationItem {
  readonly delegation_id: string;
  readonly agent_id: string;
  readonly parent_agent_id: string;
  readonly delegation_mode: "copy" | "clean" | "shared";
  readonly status: "active" | "revoked" | "expired" | "completed";
  readonly scope_prefix: string | null;
  readonly lease_expires_at: string | null;
  readonly zone_id: string | null;
  readonly intent: string;
  readonly depth: number;
  readonly can_sub_delegate: boolean;
  readonly created_at: string;
}

export interface DelegationCreateResponse {
  readonly delegation_id: string;
  readonly worker_agent_id: string;
  readonly api_key: string;
  readonly mount_table: readonly string[];
  readonly expires_at: string | null;
  readonly delegation_mode: string;
}

export interface DelegationChainEntry {
  readonly delegation_id: string;
  readonly agent_id: string;
  readonly parent_agent_id: string;
  readonly delegation_mode: string;
  readonly status: string;
  readonly depth: number;
  readonly intent: string;
  readonly created_at: string;
}

export interface DelegationChain {
  readonly chain: readonly DelegationChainEntry[];
  readonly total_depth: number;
}

export interface NamespaceDetail {
  readonly delegation_id: string;
  readonly agent_id: string;
  readonly delegation_mode: string;
  readonly scope_prefix: string | null;
  readonly removed_grants: readonly string[];
  readonly added_grants: readonly string[];
  readonly readonly_paths: readonly string[];
  readonly zone_id: string | null;
}

export interface DelegationState {
  // Data
  readonly delegations: readonly DelegationItem[];
  readonly delegationsLoading: boolean;
  readonly selectedDelegationIndex: number;
  readonly lastDelegationCreate: DelegationCreateResponse | null;
  readonly delegationChain: DelegationChain | null;
  readonly delegationChainLoading: boolean;
  readonly namespaceDetail: NamespaceDetail | null;
  readonly namespaceDetailLoading: boolean;
  readonly error: string | null;

  // Pagination
  readonly delegationsTotal: number;
  readonly delegationsLimit: number;
  readonly delegationsOffset: number;
  readonly delegationsStatusFilter: string | null;

  // Actions
  readonly fetchDelegations: (client: FetchClient, options?: {
    limit?: number;
    offset?: number;
    status?: string | null;
  }) => Promise<void>;
  readonly createDelegation: (
    request: {
      readonly worker_id: string;
      readonly worker_name: string;
      readonly namespace_mode: string;
      readonly scope_prefix?: string;
      readonly intent: string;
      readonly can_sub_delegate: boolean;
      readonly ttl_seconds?: number;
      readonly remove_grants?: readonly string[];
      readonly add_grants?: readonly string[];
      readonly readonly_paths?: readonly string[];
      readonly scope?: {
        readonly allowed_operations?: readonly string[];
        readonly resource_patterns?: readonly string[];
        readonly budget_limit?: string;
        readonly max_depth?: number;
      };
      readonly min_trust_score?: number;
    },
    client: FetchClient,
  ) => Promise<void>;
  readonly revokeDelegation: (delegationId: string, client: FetchClient) => Promise<void>;
  readonly completeDelegation: (
    delegationId: string,
    outcome: string,
    qualityScore: number | null,
    client: FetchClient,
  ) => Promise<void>;
  readonly fetchDelegationChain: (delegationId: string, client: FetchClient) => Promise<void>;
  readonly fetchNamespaceDetail: (delegationId: string, client: FetchClient) => Promise<void>;
  readonly updateNamespaceConfig: (
    delegationId: string,
    update: {
      readonly scope_prefix?: string;
      readonly remove_grants?: readonly string[];
      readonly add_grants?: readonly string[];
      readonly readonly_paths?: readonly string[];
    },
    client: FetchClient,
  ) => Promise<void>;
  readonly setSelectedDelegationIndex: (index: number) => void;
  readonly setStatusFilter: (status: string | null) => void;
}

const SOURCE = "delegation";

export const useDelegationStore = create<DelegationState>((set, get) => ({
  delegations: [],
  delegationsLoading: false,
  selectedDelegationIndex: 0,
  lastDelegationCreate: null,
  delegationChain: null,
  delegationChainLoading: false,
  namespaceDetail: null,
  namespaceDetailLoading: false,
  error: null,
  delegationsTotal: 0,
  delegationsLimit: 50,
  delegationsOffset: 0,
  delegationsStatusFilter: null,

  // =========================================================================
  // Actions — inline with error store integration (complex logic/get())
  // =========================================================================

  fetchDelegations: async (client, options) => {
    const limit = options?.limit ?? get().delegationsLimit;
    const offset = options?.offset ?? get().delegationsOffset;
    const status = options?.status !== undefined ? options.status : get().delegationsStatusFilter;

    set({ delegationsLoading: true, error: null });
    try {
      let url = `/api/v2/agents/delegate?limit=${limit}&offset=${offset}`;
      if (status) url += `&status=${encodeURIComponent(status)}`;

      const response = await client.get<{
        readonly delegations: readonly DelegationItem[];
        readonly total: number;
        readonly limit: number;
        readonly offset: number;
      }>(url);
      set({
        delegations: response.delegations,
        delegationsTotal: response.total,
        delegationsLimit: response.limit,
        delegationsOffset: response.offset,
        delegationsLoading: false,
        selectedDelegationIndex: 0,
        delegationsStatusFilter: status,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch delegations";
      set({ delegationsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  createDelegation: async (request, client) => {
    set({ delegationsLoading: true, error: null });
    try {
      const response = await client.post<DelegationCreateResponse>(
        "/api/v2/agents/delegate",
        request,
      );
      set({ lastDelegationCreate: response, delegationsLoading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to create delegation";
      set({ delegationsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
      return;
    }
    // Re-fetch list
    try {
      const { delegationsLimit, delegationsOffset, delegationsStatusFilter } = get();
      let url = `/api/v2/agents/delegate?limit=${delegationsLimit}&offset=${delegationsOffset}`;
      if (delegationsStatusFilter) url += `&status=${encodeURIComponent(delegationsStatusFilter)}`;
      const listResponse = await client.get<{
        readonly delegations: readonly DelegationItem[];
        readonly total: number;
      }>(url);
      set({ delegations: listResponse.delegations, selectedDelegationIndex: 0 });
    } catch {
      // Non-critical
    }
  },

  revokeDelegation: async (delegationId, client) => {
    set({ delegationsLoading: true, error: null });
    try {
      await client.delete(`/api/v2/agents/delegate/${encodeURIComponent(delegationId)}`);
      set((state) => ({
        delegations: state.delegations.map((d) =>
          d.delegation_id === delegationId ? { ...d, status: "revoked" } : d,
        ),
        delegationsLoading: false,
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to revoke delegation";
      set({ delegationsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  completeDelegation: async (delegationId, outcome, qualityScore, client) => {
    set({ delegationsLoading: true, error: null });
    try {
      const body: { outcome: string; quality_score?: number } = { outcome };
      if (qualityScore !== null) body.quality_score = qualityScore;
      await client.post(
        `/api/v2/agents/delegate/${encodeURIComponent(delegationId)}/complete`,
        body,
      );
      set((state) => ({
        delegations: state.delegations.map((d) =>
          d.delegation_id === delegationId ? { ...d, status: "completed" } : d,
        ),
        delegationsLoading: false,
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to complete delegation";
      set({ delegationsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  // =========================================================================
  // Actions migrated to createApiAction (Decision 6A)
  // =========================================================================

  fetchDelegationChain: createApiAction<DelegationState, [string, FetchClient]>(set, {
    loadingKey: "delegationChainLoading",
    source: SOURCE,
    action: async (delegationId, client) => {
      const response = await client.get<DelegationChain>(
        `/api/v2/agents/delegate/${encodeURIComponent(delegationId)}/chain`,
      );
      return { delegationChain: response };
    },
  }),

  fetchNamespaceDetail: createApiAction<DelegationState, [string, FetchClient]>(set, {
    loadingKey: "namespaceDetailLoading",
    source: SOURCE,
    action: async (delegationId, client) => {
      const response = await client.get<NamespaceDetail>(
        `/api/v2/agents/delegate/${encodeURIComponent(delegationId)}/namespace`,
      );
      return { namespaceDetail: response };
    },
  }),

  updateNamespaceConfig: createApiAction<DelegationState, [string, { readonly scope_prefix?: string; readonly remove_grants?: readonly string[]; readonly add_grants?: readonly string[]; readonly readonly_paths?: readonly string[] }, FetchClient]>(set, {
    loadingKey: "namespaceDetailLoading",
    source: SOURCE,
    action: async (delegationId, update, client) => {
      const response = await client.patch<NamespaceDetail>(
        `/api/v2/agents/delegate/${encodeURIComponent(delegationId)}/namespace`,
        update,
      );
      return { namespaceDetail: response };
    },
  }),

  setSelectedDelegationIndex: (index) => {
    set({ selectedDelegationIndex: index });
  },

  setStatusFilter: (status) => {
    set({ delegationsStatusFilter: status });
  },
}));
