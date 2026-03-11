/**
 * Zustand store for the Access Control (ReBAC) panel:
 * manifests, permission checks, governance alerts, reputation, credentials.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

// =============================================================================
// Types
// =============================================================================

export interface AccessManifest {
  readonly manifest_id: string;
  readonly subject: string;
  readonly relation: string;
  readonly object: string;
  readonly zone_id: string | null;
  readonly granted_at: string;
  readonly expires_at: string | null;
  readonly granted_by: string;
}

export interface PermissionCheck {
  readonly allowed: boolean;
  readonly reason: string;
  readonly checked_at: string;
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

export interface ReputationScore {
  readonly agent_id: string;
  readonly score: number;
  readonly trust_level: string;
  readonly last_updated: string;
}

export interface LeaderboardEntry {
  readonly rank: number;
  readonly agent_id: string;
  readonly score: number;
  readonly trust_level: string;
}

export interface Credential {
  readonly credential_id: string;
  readonly type: string;
  readonly issuer: string;
  readonly subject: string;
  readonly issued_at: string;
  readonly expires_at: string | null;
  readonly status: "active" | "revoked" | "expired";
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

  // Reputation
  readonly scores: readonly ReputationScore[];
  readonly scoresLoading: boolean;
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
    subject: string,
    relation: string,
    object: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly fetchAlerts: (client: FetchClient) => Promise<void>;
  readonly fetchScores: (client: FetchClient) => Promise<void>;
  readonly fetchLeaderboard: (client: FetchClient) => Promise<void>;
  readonly fetchCredentials: (client: FetchClient) => Promise<void>;
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
  scores: [],
  scoresLoading: false,
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
      }>("/api/v2/access/manifests");
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

  checkPermission: async (subject, relation, object, client) => {
    set({ permissionCheckLoading: true, error: null });
    try {
      const response = await client.post<{
        readonly allowed: boolean;
        readonly reason: string;
      }>("/api/v2/access/check", { subject, relation, object });
      set({
        lastPermissionCheck: {
          allowed: response.allowed,
          reason: response.reason,
          checked_at: new Date().toISOString(),
        },
        permissionCheckLoading: false,
      });
    } catch (err) {
      set({
        permissionCheckLoading: false,
        error: err instanceof Error ? err.message : "Failed to check permission",
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

  fetchScores: async (client) => {
    set({ scoresLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly scores: readonly ReputationScore[];
      }>("/api/v2/governance/reputation/scores");
      set({
        scores: response.scores,
        scoresLoading: false,
      });
    } catch (err) {
      set({
        scoresLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch reputation scores",
      });
    }
  },

  fetchLeaderboard: async (client) => {
    set({ leaderboardLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly entries: readonly LeaderboardEntry[];
      }>("/api/v2/governance/leaderboard");
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

  fetchCredentials: async (client) => {
    set({ credentialsLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly credentials: readonly Credential[];
      }>("/api/v2/credentials");
      set({
        credentials: response.credentials,
        credentialsLoading: false,
      });
    } catch (err) {
      set({
        credentialsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch credentials",
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
