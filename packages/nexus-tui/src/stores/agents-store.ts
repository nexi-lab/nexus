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

export type AgentTab = "status" | "delegations" | "inbox";

// =============================================================================
// Store
// =============================================================================

export interface AgentsState {
  // Agent list (we track agents we've queried)
  readonly knownAgents: readonly string[];
  readonly selectedAgentId: string | null;
  readonly selectedAgentIndex: number;

  // Detail tabs
  readonly activeTab: AgentTab;

  // Status
  readonly agentStatus: AgentStatus | null;
  readonly agentSpec: AgentSpec | null;
  readonly agentIdentity: AgentIdentity | null;
  readonly statusLoading: boolean;

  // Delegations
  readonly delegations: readonly DelegationItem[];
  readonly delegationsLoading: boolean;
  readonly selectedDelegationIndex: number;

  // Inbox
  readonly inboxMessages: readonly InboxMessage[];
  readonly inboxCount: number;
  readonly inboxLoading: boolean;

  // Error
  readonly error: string | null;

  // Actions
  readonly setSelectedAgentId: (id: string) => void;
  readonly setSelectedAgentIndex: (index: number) => void;
  readonly setActiveTab: (tab: AgentTab) => void;
  readonly addKnownAgent: (id: string) => void;
  readonly fetchAgentStatus: (agentId: string, client: FetchClient) => Promise<void>;
  readonly fetchAgentSpec: (agentId: string, client: FetchClient) => Promise<void>;
  readonly fetchAgentIdentity: (agentId: string, client: FetchClient) => Promise<void>;
  readonly fetchDelegations: (client: FetchClient) => Promise<void>;
  readonly fetchInbox: (agentId: string, client: FetchClient) => Promise<void>;
  readonly revokeDelegation: (delegationId: string, client: FetchClient) => Promise<void>;
  readonly setSelectedDelegationIndex: (index: number) => void;
}

export const useAgentsStore = create<AgentsState>((set, get) => ({
  knownAgents: [],
  selectedAgentId: null,
  selectedAgentIndex: 0,
  activeTab: "status",
  agentStatus: null,
  agentSpec: null,
  agentIdentity: null,
  statusLoading: false,
  delegations: [],
  delegationsLoading: false,
  selectedDelegationIndex: 0,
  inboxMessages: [],
  inboxCount: 0,
  inboxLoading: false,
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

  setSelectedDelegationIndex: (index) => {
    set({ selectedDelegationIndex: index });
  },
}));
