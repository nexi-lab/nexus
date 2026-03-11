/**
 * Zustand store for the Access Control panel:
 * manifests, permission evaluation, governance alerts, reputation leaderboard, credentials.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

// =============================================================================
// Types
// =============================================================================

export interface ManifestEntry {
  readonly tool_pattern: string;
  readonly permission: string;
  readonly max_calls_per_minute: number | null;
}

export interface AccessManifest {
  readonly manifest_id: string;
  readonly agent_id: string;
  readonly zone_id: string;
  readonly name: string;
  readonly entries: readonly ManifestEntry[];
  readonly status: string;
  readonly valid_from: string;
  readonly valid_until: string;
}

export interface PermissionCheck {
  readonly tool_name: string;
  readonly permission: string;
  readonly agent_id: string;
  readonly manifest_id: string;
}

export interface GovernanceAlert {
  readonly alert_id: string;
  readonly severity: "info" | "warning" | "critical";
  readonly category: string;
  readonly message: string;
  readonly agent_id: string | null;
  readonly created_at: string;
  readonly resolved: boolean;
}

export interface LeaderboardEntry {
  readonly agent_id: string;
  readonly context: string;
  readonly window: string;
  readonly composite_score: number;
  readonly composite_confidence: number;
  readonly total_interactions: number;
  readonly positive_interactions: number;
  readonly negative_interactions: number;
  readonly global_trust_score: number | null;
  readonly zone_id: string;
  readonly updated_at: string;
}

export interface Credential {
  readonly credential_id: string;
  readonly issuer_did: string;
  readonly subject_did: string;
  readonly subject_agent_id: string;
  readonly is_active: boolean;
  readonly created_at: string;
  readonly expires_at: string;
  readonly revoked_at: string | null;
  readonly delegation_depth: number;
}

/** Matches backend DisputeResponse from reputation.py. */
export interface Dispute {
  readonly id: string;
  readonly exchange_id: string;
  readonly zone_id: string;
  readonly complainant_agent_id: string;
  readonly respondent_agent_id: string;
  readonly status: string;
  readonly tier: number;
  readonly reason: string;
  readonly resolution: string | null;
  readonly resolution_evidence_hash: string | null;
  readonly escrow_amount: string | null;
  readonly escrow_released: boolean;
  readonly filed_at: string;
  readonly resolved_at: string | null;
  readonly appeal_deadline: string | null;
}

export type AccessTab = "manifests" | "alerts" | "reputation" | "credentials" | "disputes";

// =============================================================================
// Store
// =============================================================================

export interface AccessState {
  // Manifests
  readonly manifests: readonly AccessManifest[];
  readonly selectedManifestIndex: number;
  readonly manifestsLoading: boolean;

  // Permission check
  readonly lastPermissionCheck: PermissionCheck | null;
  readonly permissionCheckLoading: boolean;

  // Governance alerts
  readonly alerts: readonly GovernanceAlert[];
  readonly alertsLoading: boolean;

  // Reputation leaderboard
  readonly leaderboard: readonly LeaderboardEntry[];
  readonly leaderboardLoading: boolean;

  // Credentials
  readonly credentials: readonly Credential[];
  readonly credentialsLoading: boolean;

  // Disputes
  readonly disputes: readonly Dispute[];
  readonly disputesLoading: boolean;
  readonly selectedDisputeIndex: number;

  // UI state
  readonly activeTab: AccessTab;
  readonly error: string | null;

  // Actions
  readonly fetchManifests: (client: FetchClient) => Promise<void>;
  readonly checkPermission: (
    manifestId: string,
    toolName: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly fetchAlerts: (client: FetchClient) => Promise<void>;
  readonly fetchLeaderboard: (client: FetchClient) => Promise<void>;
  readonly fetchCredentials: (
    agentId: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly fetchDispute: (disputeId: string, client: FetchClient) => Promise<void>;
  readonly fileDispute: (
    exchangeId: string,
    complainantId: string,
    respondentId: string,
    reason: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly resolveDispute: (disputeId: string, resolution: string, client: FetchClient) => Promise<void>;
  readonly setActiveTab: (tab: AccessTab) => void;
  readonly setSelectedManifestIndex: (index: number) => void;
  readonly setSelectedDisputeIndex: (index: number) => void;
}

export const useAccessStore = create<AccessState>((set) => ({
  manifests: [],
  selectedManifestIndex: 0,
  manifestsLoading: false,
  lastPermissionCheck: null,
  permissionCheckLoading: false,
  alerts: [],
  alertsLoading: false,
  leaderboard: [],
  leaderboardLoading: false,
  credentials: [],
  credentialsLoading: false,
  disputes: [],
  disputesLoading: false,
  selectedDisputeIndex: 0,
  activeTab: "manifests",
  error: null,

  fetchManifests: async (client) => {
    set({ manifestsLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly manifests: readonly AccessManifest[];
        readonly offset: number;
        readonly limit: number;
        readonly count: number;
      }>("/api/v2/access-manifests");
      set({
        manifests: response.manifests,
        manifestsLoading: false,
        selectedManifestIndex: 0,
      });
    } catch (err) {
      set({
        manifestsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch manifests",
      });
    }
  },

  checkPermission: async (manifestId, toolName, client) => {
    set({ permissionCheckLoading: true, error: null });
    try {
      const response = await client.post<{
        readonly tool_name: string;
        readonly permission: string;
        readonly agent_id: string;
        readonly manifest_id: string;
      }>(`/api/v2/access-manifests/${manifestId}/evaluate`, {
        tool_name: toolName,
      });
      set({
        lastPermissionCheck: {
          tool_name: response.tool_name,
          permission: response.permission,
          agent_id: response.agent_id,
          manifest_id: response.manifest_id,
        },
        permissionCheckLoading: false,
      });
    } catch (err) {
      set({
        permissionCheckLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to evaluate permission",
      });
    }
  },

  fetchAlerts: async (client) => {
    set({ alertsLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly alerts: readonly GovernanceAlert[];
      }>("/api/v2/governance/alerts");
      set({
        alerts: response.alerts,
        alertsLoading: false,
      });
    } catch (err) {
      set({
        alertsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch alerts",
      });
    }
  },

  fetchLeaderboard: async (client) => {
    set({ leaderboardLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly entries: readonly LeaderboardEntry[];
      }>("/api/v2/reputation/leaderboard");
      set({
        leaderboard: response.entries,
        leaderboardLoading: false,
      });
    } catch (err) {
      set({
        leaderboardLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch leaderboard",
      });
    }
  },

  fetchCredentials: async (agentId, client) => {
    set({ credentialsLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly agent_id: string;
        readonly count: number;
        readonly credentials: readonly Credential[];
      }>(`/api/v2/agents/${agentId}/credentials`);
      set({
        credentials: response.credentials,
        credentialsLoading: false,
      });
    } catch (err) {
      set({
        credentialsLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch credentials",
      });
    }
  },

  fetchDispute: async (disputeId, client) => {
    set({ disputesLoading: true, error: null });
    try {
      const dispute = await client.get<Dispute>(
        `/api/v2/disputes/${encodeURIComponent(disputeId)}`,
      );
      set((state) => {
        const existing = state.disputes.findIndex((d) => d.id === dispute.id);
        const updated = existing >= 0
          ? state.disputes.map((d, i) => (i === existing ? dispute : d))
          : [...state.disputes, dispute];
        return { disputes: updated, disputesLoading: false };
      });
    } catch (err) {
      set({
        disputesLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch dispute",
      });
    }
  },

  fileDispute: async (exchangeId, complainantId, respondentId, reason, client) => {
    set({ disputesLoading: true, error: null });
    try {
      const dispute = await client.post<Dispute>(
        `/api/v2/exchanges/${encodeURIComponent(exchangeId)}/dispute`,
        {
          complainant_agent_id: complainantId,
          respondent_agent_id: respondentId,
          reason,
        },
      );
      set((state) => ({
        disputes: [...state.disputes, dispute],
        disputesLoading: false,
      }));
    } catch (err) {
      set({
        disputesLoading: false,
        error: err instanceof Error ? err.message : "Failed to file dispute",
      });
    }
  },

  resolveDispute: async (disputeId, resolution, client) => {
    set({ disputesLoading: true, error: null });
    try {
      const updated = await client.post<Dispute>(
        `/api/v2/disputes/${encodeURIComponent(disputeId)}/resolve`,
        { resolution },
      );
      set((state) => ({
        disputes: state.disputes.map((d) => (d.id === disputeId ? updated : d)),
        disputesLoading: false,
      }));
    } catch (err) {
      set({
        disputesLoading: false,
        error: err instanceof Error ? err.message : "Failed to resolve dispute",
      });
    }
  },

  setActiveTab: (tab) => {
    set({ activeTab: tab, error: null });
  },

  setSelectedManifestIndex: (index) => {
    set({ selectedManifestIndex: index });
  },

  setSelectedDisputeIndex: (index) => {
    set({ selectedDisputeIndex: index });
  },
}));
