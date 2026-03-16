/**
 * Zustand store for the Agents panel: status, delegations, inbox, identity.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

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

  fetchAgentStatus: async (agentId, client) => {
    set({ statusLoading: true, error: null });
    try {
      const response = await client.get<Omit<AgentStatus, "agent_id">>(
        `/api/v2/agents/${encodeURIComponent(agentId)}/status`,
      );
      set({
        agentStatus: { ...response, agent_id: agentId },
        statusLoading: false,
      });
    } catch (err) {
      set({
        statusLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch agent status",
      });
    }
  },

  fetchAgentSpec: async (agentId, client) => {
    try {
      const response = await client.get<AgentSpec>(
        `/api/v2/agents/${encodeURIComponent(agentId)}/spec`,
      );
      set({ agentSpec: response, error: null });
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to fetch agent spec",
      });
    }
  },

  fetchAgentIdentity: async (agentId, client) => {
    try {
      const response = await client.get<AgentIdentity>(
        `/api/v2/agents/${encodeURIComponent(agentId)}/identity`,
      );
      set({ agentIdentity: response, error: null });
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to fetch agent identity",
      });
    }
  },

  fetchDelegations: async (client) => {
    set({ delegationsLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly delegations: readonly DelegationItem[];
        readonly total: number;
      }>("/api/v2/agents/delegate?limit=50&offset=0");
      set({
        delegations: response.delegations,
        delegationsLoading: false,
        selectedDelegationIndex: 0,
      });
    } catch (err) {
      set({
        delegationsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch delegations",
      });
    }
  },

  fetchInbox: async (agentId, client) => {
    set({ inboxLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly agent_id: string;
        readonly messages: readonly InboxMessage[];
        readonly total: number;
      }>(`/api/v2/ipc/inbox/${encodeURIComponent(agentId)}`);
      set({
        inboxMessages: response.messages,
        inboxCount: response.total,
        inboxLoading: false,
      });
    } catch (err) {
      set({
        inboxLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch inbox",
      });
    }
  },

  fetchTrajectories: async (agentId, client) => {
    set({ trajectoriesLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly trajectories: readonly TrajectoryItem[];
      }>(`/api/v2/trajectories?agent_id=${encodeURIComponent(agentId)}&limit=20`);
      set({
        trajectories: response.trajectories ?? [],
        trajectoriesLoading: false,
      });
    } catch (err) {
      set({
        trajectoriesLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch trajectories",
      });
    }
  },

  revokeDelegation: async (delegationId, client) => {
    try {
      await client.delete(`/api/v2/agents/delegate/${encodeURIComponent(delegationId)}`);
      // Remove from local list
      set((state) => ({
        delegations: state.delegations.map((d) =>
          d.delegation_id === delegationId ? { ...d, status: "revoked" as const } : d,
        ),
        error: null,
      }));
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to revoke delegation",
      });
    }
  },

  fetchAgents: async (zoneId, client) => {
    set({ agentsLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly agents: readonly AgentListItem[];
      }>(`/api/v2/agents?zone_id=${encodeURIComponent(zoneId)}&limit=50&offset=0`);
      set({ agents: response.agents, agentsLoading: false });
    } catch (err) {
      set({
        agentsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch agents",
      });
    }
  },

  warmupAgent: async (agentId, client) => {
    set({ error: null });
    try {
      await client.post(`/api/v2/agents/${encodeURIComponent(agentId)}/warmup`, {});
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to warmup agent",
      });
    }
  },

  evictAgent: async (agentId, client) => {
    set({ error: null });
    try {
      await client.post(`/api/v2/agents/${encodeURIComponent(agentId)}/evict`, {});
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to evict agent",
      });
    }
  },

  verifyAgent: async (agentId, client) => {
    set({ error: null });
    try {
      await client.post(`/api/v2/agents/${encodeURIComponent(agentId)}/verify`, {});
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to verify agent",
      });
    }
  },

  fetchTrustScore: async (agentId, client) => {
    set({ trustScoreLoading: true, error: null });
    try {
      const response = await client.get<{ readonly trust_score: number }>(
        `/api/v2/agents/${encodeURIComponent(agentId)}/trust-score`,
      );
      set({ trustScore: response.trust_score, trustScoreLoading: false });
    } catch (err) {
      set({
        trustScoreLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch trust score",
      });
    }
  },

  fetchAgentReputation: async (agentId, client) => {
    set({ reputationLoading: true, error: null });
    try {
      const response = await client.get<unknown>(
        `/api/v2/agents/${encodeURIComponent(agentId)}/reputation`,
      );
      set({ reputation: response, reputationLoading: false });
    } catch (err) {
      set({
        reputationLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch agent reputation",
      });
    }
  },

  setSelectedDelegationIndex: (index) => {
    set({ selectedDelegationIndex: index });
  },
}));
