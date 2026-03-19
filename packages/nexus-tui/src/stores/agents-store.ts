/**
 * Zustand store for the Agents panel: status, delegations, inbox, identity.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";
import { createApiAction, categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";
export type { DelegationItem } from "./delegation-store.js";
import type { DelegationItem } from "./delegation-store.js";

// =============================================================================
// Types
// =============================================================================

export type AgentPhase =
  | "warming"
  | "ready"
  | "active"
  | "thinking"
  | "idle"
  | "suspended"
  | "evicted";

export interface AgentCondition {
  readonly type: string;
  readonly status: string;
  readonly reason: string;
  readonly message: string;
  readonly last_transition: string;
}

export interface AgentResourceUsage {
  readonly tokens_used: number;
  readonly storage_used_mb: number;
  readonly context_usage_pct: number;
}

export interface AgentStatus {
  readonly agent_id: string;
  readonly phase: AgentPhase;
  readonly observed_generation: number;
  readonly conditions: readonly AgentCondition[];
  readonly resource_usage: AgentResourceUsage;
  readonly last_heartbeat: string | null;
  readonly last_activity: string | null;
  readonly inbox_depth: number;
  readonly context_usage_pct: number;
}

export interface AgentSpec {
  readonly agent_type: string;
  readonly capabilities: readonly string[];
  readonly qos_class: string;
  readonly zone_affinity: string | null;
  readonly spec_generation: number;
}

export interface AgentIdentity {
  readonly agent_id: string;
  readonly key_id: string;
  readonly did: string;
  readonly algorithm: string;
  readonly public_key_hex: string;
  readonly created_at: string | null;
  readonly expires_at: string | null;
}

// DelegationItem re-exported from delegation-store.ts (canonical source)

export interface InboxMessage {
  readonly filename: string;
}

export interface AgentListItem {
  readonly agent_id: string;
  readonly owner_id: string;
  readonly zone_id: string | null;
  readonly name: string | null;
  readonly state: string;
  readonly generation: number;
}

export interface TrajectoryItem {
  readonly trace_id: string;
  readonly agent_id: string;
  readonly status: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;
  readonly step_count: number;
}

export type AgentTab = "status" | "delegations" | "inbox" | "trajectories";

// =============================================================================
// Store
// =============================================================================

export interface AgentsState {
  // Agent list (we track agents we've queried)
  readonly knownAgents: readonly string[];
  readonly selectedAgentId: string | null;
  readonly selectedAgentIndex: number;

  // Fetched agent list
  readonly agents: readonly AgentListItem[];
  readonly agentsLoading: boolean;

  // Detail tabs
  readonly activeTab: AgentTab;

  // Status
  readonly agentStatus: AgentStatus | null;
  readonly agentSpec: AgentSpec | null;
  readonly agentIdentity: AgentIdentity | null;
  readonly statusLoading: boolean;

  // Trust score
  readonly trustScore: number | null;
  readonly trustScoreLoading: boolean;

  // Reputation
  readonly reputation: unknown | null;
  readonly reputationLoading: boolean;

  // Delegations
  readonly delegations: readonly DelegationItem[];
  readonly delegationsLoading: boolean;
  readonly selectedDelegationIndex: number;

  // Inbox
  readonly inboxMessages: readonly InboxMessage[];
  readonly inboxCount: number;
  readonly processedMessages: readonly InboxMessage[];
  readonly deadLetterMessages: readonly InboxMessage[];
  readonly inboxLoading: boolean;

  // Trajectories
  readonly trajectories: readonly TrajectoryItem[];
  readonly trajectoriesLoading: boolean;

  // Error
  readonly error: string | null;

  // Actions
  readonly setSelectedAgentId: (id: string) => void;
  readonly setSelectedAgentIndex: (index: number) => void;
  readonly setActiveTab: (tab: AgentTab) => void;
  readonly addKnownAgent: (id: string) => void;
  readonly fetchAgents: (zoneId: string, client: FetchClient) => Promise<void>;
  readonly fetchAgentStatus: (agentId: string, client: FetchClient) => Promise<void>;
  readonly fetchAgentSpec: (agentId: string, client: FetchClient) => Promise<void>;
  readonly fetchAgentIdentity: (agentId: string, client: FetchClient) => Promise<void>;
  readonly fetchDelegations: (client: FetchClient) => Promise<void>;
  readonly fetchInbox: (agentId: string, client: FetchClient) => Promise<void>;
  readonly fetchTrajectories: (agentId: string, client: FetchClient) => Promise<void>;
  readonly revokeDelegation: (delegationId: string, client: FetchClient) => Promise<void>;
  readonly warmupAgent: (agentId: string, client: FetchClient) => Promise<void>;
  readonly evictAgent: (agentId: string, client: FetchClient) => Promise<void>;
  readonly verifyAgent: (agentId: string, client: FetchClient) => Promise<void>;
  readonly fetchTrustScore: (agentId: string, client: FetchClient) => Promise<void>;
  readonly fetchAgentReputation: (agentId: string, client: FetchClient) => Promise<void>;
  readonly setSelectedDelegationIndex: (index: number) => void;
}

const SOURCE = "agents";

export const useAgentsStore = create<AgentsState>((set, get) => ({
  knownAgents: [],
  selectedAgentId: null,
  selectedAgentIndex: 0,
  agents: [],
  agentsLoading: false,
  activeTab: "status",
  agentStatus: null,
  agentSpec: null,
  agentIdentity: null,
  statusLoading: false,
  trustScore: null,
  trustScoreLoading: false,
  reputation: null,
  reputationLoading: false,
  delegations: [],
  delegationsLoading: false,
  selectedDelegationIndex: 0,
  inboxMessages: [],
  processedMessages: [],
  deadLetterMessages: [],
  inboxCount: 0,
  inboxLoading: false,
  trajectories: [],
  trajectoriesLoading: false,
  error: null,

  setSelectedAgentId: (id) => {
    set({ selectedAgentId: id, error: null });
  },

  setSelectedAgentIndex: (index) => {
    set({ selectedAgentIndex: index });
  },

  setActiveTab: (tab) => {
    set({ activeTab: tab });
  },

  addKnownAgent: (id) => {
    const { knownAgents } = get();
    if (knownAgents.includes(id)) return;
    set({ knownAgents: [...knownAgents, id] });
  },

  // =========================================================================
  // Actions migrated to createApiAction (Decision 5A)
  // =========================================================================

  fetchAgentStatus: createApiAction<AgentsState, [string, FetchClient]>(set, {
    loadingKey: "statusLoading",
    source: SOURCE,
    action: async (agentId, client) => {
      const response = await client.get<Omit<AgentStatus, "agent_id">>(
        `/api/v2/agents/${encodeURIComponent(agentId)}/status`,
      );
      return { agentStatus: { ...response, agent_id: agentId } };
    },
  }),

  fetchAgents: createApiAction<AgentsState, [string, FetchClient]>(set, {
    loadingKey: "agentsLoading",
    source: SOURCE,
    action: async (zoneId, client) => {
      const response = await client.get<{
        readonly agents: readonly AgentListItem[];
      }>(`/api/v2/agents?zone_id=${encodeURIComponent(zoneId)}&limit=50&offset=0`);
      return { agents: response.agents };
    },
  }),

  fetchDelegations: createApiAction<AgentsState, [FetchClient]>(set, {
    loadingKey: "delegationsLoading",
    source: SOURCE,
    action: async (client) => {
      const response = await client.get<{
        readonly delegations: readonly DelegationItem[];
        readonly total: number;
      }>("/api/v2/agents/delegate?limit=50&offset=0");
      return { delegations: response.delegations, selectedDelegationIndex: 0 };
    },
  }),

  fetchInbox: createApiAction<AgentsState, [string, FetchClient]>(set, {
    loadingKey: "inboxLoading",
    source: SOURCE,
    action: async (agentId, client) => {
      const encodedId = encodeURIComponent(agentId);
      type InboxResp = { readonly messages: readonly InboxMessage[]; readonly total: number };
      type FilesResp = { readonly items: readonly { readonly name: string }[] };

      // Fetch inbox via IPC endpoint, processed/dead_letter via files API
      const listFolder = (folder: string): Promise<readonly InboxMessage[]> =>
        client.get<FilesResp>(
          `/api/v2/files/list?path=${encodeURIComponent(`/agents/${agentId}/${folder}`)}&limit=100`,
        ).then((r) => r.items.filter((f) => f.name.endsWith(".json")).map((f) => ({ filename: f.name })))
          .catch(() => []);

      const [inboxResp, processedMsgs, deadLetterMsgs] = await Promise.all([
        client.get<InboxResp>(`/api/v2/ipc/inbox/${encodedId}`)
          .catch(() => ({ messages: [] as InboxMessage[], total: 0 })),
        listFolder("processed"),
        listFolder("dead_letter"),
      ]);
      return {
        inboxMessages: inboxResp.messages,
        inboxCount: inboxResp.total,
        processedMessages: processedMsgs,
        deadLetterMessages: deadLetterMsgs,
      };
    },
  }),

  fetchTrajectories: createApiAction<AgentsState, [string, FetchClient]>(set, {
    loadingKey: "trajectoriesLoading",
    source: SOURCE,
    action: async (agentId, client) => {
      const response = await client.get<{
        readonly trajectories: readonly TrajectoryItem[];
      }>(`/api/v2/trajectories?agent_id=${encodeURIComponent(agentId)}&limit=20`);
      return { trajectories: response.trajectories ?? [] };
    },
  }),

  fetchTrustScore: createApiAction<AgentsState, [string, FetchClient]>(set, {
    loadingKey: "trustScoreLoading",
    source: SOURCE,
    action: async (agentId, client) => {
      const response = await client.get<{ readonly trust_score: number }>(
        `/api/v2/agents/${encodeURIComponent(agentId)}/trust-score`,
      );
      return { trustScore: response.trust_score };
    },
  }),

  fetchAgentReputation: createApiAction<AgentsState, [string, FetchClient]>(set, {
    loadingKey: "reputationLoading",
    source: SOURCE,
    action: async (agentId, client) => {
      const response = await client.get<unknown>(
        `/api/v2/agents/${encodeURIComponent(agentId)}/reputation`,
      );
      return { reputation: response };
    },
  }),

  // =========================================================================
  // Actions without loading keys — inline but with error store integration
  // =========================================================================

  fetchAgentSpec: async (agentId, client) => {
    try {
      const response = await client.get<AgentSpec>(
        `/api/v2/agents/${encodeURIComponent(agentId)}/spec`,
      );
      set({ agentSpec: response, error: null });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch agent spec";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchAgentIdentity: async (agentId, client) => {
    try {
      const response = await client.get<AgentIdentity>(
        `/api/v2/agents/${encodeURIComponent(agentId)}/identity`,
      );
      set({ agentIdentity: response, error: null });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch agent identity";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  revokeDelegation: async (delegationId, client) => {
    try {
      await client.delete(`/api/v2/agents/delegate/${encodeURIComponent(delegationId)}`);
      set((state) => ({
        delegations: state.delegations.map((d) =>
          d.delegation_id === delegationId ? { ...d, status: "revoked" as const } : d,
        ),
        error: null,
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to revoke delegation";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  warmupAgent: async (agentId, client) => {
    set({ error: null });
    try {
      await client.post(`/api/v2/agents/${encodeURIComponent(agentId)}/warmup`, {});
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to warmup agent";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  evictAgent: async (agentId, client) => {
    set({ error: null });
    try {
      await client.post(`/api/v2/agents/${encodeURIComponent(agentId)}/evict`, {});
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to evict agent";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  verifyAgent: async (agentId, client) => {
    set({ error: null });
    try {
      await client.post(`/api/v2/agents/${encodeURIComponent(agentId)}/verify`, {});
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to verify agent";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  setSelectedDelegationIndex: (index) => {
    set({ selectedDelegationIndex: index });
  },
}));
