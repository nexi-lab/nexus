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

export type AccessTab = "manifests" | "alerts" | "reputation" | "credentials";

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
  readonly setActiveTab: (tab: AccessTab) => void;
  readonly setSelectedManifestIndex: (index: number) => void;
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

  setActiveTab: (tab) => {
    set({ activeTab: tab, error: null });
  },

  setSelectedManifestIndex: (index) => {
    set({ selectedManifestIndex: index });
  },
}));
