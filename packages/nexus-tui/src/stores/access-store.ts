/**
 * Zustand store for the Access Control panel:
 * manifests (+ tuple entries), permission evaluation, governance alerts,
 * reputation leaderboard, credentials, disputes, fraud scores.
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

/** List endpoint returns summary (no entries); detail endpoint includes entries. */
export interface AccessManifest {
  readonly manifest_id: string;
  readonly agent_id: string;
  readonly zone_id: string;
  readonly name: string;
  readonly entries?: readonly ManifestEntry[];
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

/** Matches backend anomaly alert from governance.py:128-139. */
export interface GovernanceAlert {
  readonly alert_id: string;
  readonly agent_id: string;
  readonly zone_id: string;
  readonly severity: string;
  readonly alert_type: string;
  readonly details: unknown;
  readonly resolved: boolean;
  readonly created_at: string | null;
}

/** Matches backend DelegationListItem from delegation.py:135-149. */
export interface DelegationItem {
  readonly delegation_id: string;
  readonly agent_id: string;
  readonly parent_agent_id: string;
  readonly delegation_mode: string;
  readonly status: string;
  readonly scope_prefix: string | null;
  readonly lease_expires_at: string | null;
  readonly zone_id: string | null;
  readonly intent: string;
  readonly depth: number;
  readonly can_sub_delegate: boolean;
  readonly created_at: string;
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

/** Matches backend FraudScoreResponse from governance.py:177. */
export interface FraudScore {
  readonly agent_id: string;
  readonly zone_id: string;
  readonly score: number;
  readonly components: Readonly<Record<string, number>>;
  readonly computed_at: string;
}

/** Matches backend DelegateResponse from delegation.py. */
export interface DelegationCreateResponse {
  readonly delegation_id: string;
  readonly worker_agent_id: string;
  readonly api_key: string;
  readonly mount_table: readonly string[];
  readonly expires_at: string | null;
  readonly delegation_mode: string;
}

/** Matches backend DelegationChainLink from delegation.py. */
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

/** Matches backend DelegationChainResponse from delegation.py. */
export interface DelegationChain {
  readonly chain: readonly DelegationChainEntry[];
  readonly total_depth: number;
}

/** Matches backend governance check result from governance.py. */
export interface GovernanceCheckResult {
  readonly allowed: boolean;
  readonly constraint_type: string | null;
  readonly reason: string;
  readonly edge_id: string;
}

export type AccessTab =
  | "manifests"
  | "alerts"
  | "reputation"
  | "credentials"
  | "disputes"
  | "fraud"
  | "delegations";

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
  readonly selectedAlertIndex: number;

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

  // Fraud scores
  readonly fraudScores: readonly FraudScore[];
  readonly fraudScoresLoading: boolean;
  readonly selectedFraudIndex: number;

  // Delegations
  readonly delegations: readonly DelegationItem[];
  readonly delegationsLoading: boolean;
  readonly selectedDelegationIndex: number;
  readonly lastDelegationCreate: DelegationCreateResponse | null;
  readonly delegationChain: DelegationChain | null;
  readonly delegationChainLoading: boolean;

  // Governance check
  readonly governanceCheck: GovernanceCheckResult | null;
  readonly governanceCheckLoading: boolean;

  // UI state
  readonly activeTab: AccessTab;
  readonly error: string | null;

  // Actions — manifests
  readonly fetchManifests: (client: FetchClient) => Promise<void>;
  readonly fetchManifestDetail: (manifestId: string, client: FetchClient) => Promise<void>;
  readonly checkPermission: (
    manifestId: string,
    toolName: string,
    client: FetchClient,
  ) => Promise<void>;

  // Actions — alerts
  readonly fetchAlerts: (zoneId: string | undefined, client: FetchClient) => Promise<void>;
  readonly resolveAlert: (alertId: string, resolvedBy: string, zoneId: string | undefined, client: FetchClient) => Promise<void>;

  // Actions — reputation
  readonly fetchLeaderboard: (client: FetchClient) => Promise<void>;

  // Actions — credentials
  readonly fetchCredentials: (agentId: string, client: FetchClient) => Promise<void>;

  // Actions — disputes
  readonly fetchDispute: (disputeId: string, client: FetchClient) => Promise<void>;
  readonly fileDispute: (
    exchangeId: string,
    complainantId: string,
    respondentId: string,
    reason: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly resolveDispute: (disputeId: string, resolution: string, client: FetchClient) => Promise<void>;

  // Actions — fraud scores
  readonly fetchFraudScores: (zoneId: string | undefined, client: FetchClient) => Promise<void>;
  readonly computeFraudScores: (zoneId: string | undefined, client: FetchClient) => Promise<void>;

  // Actions — delegations
  readonly fetchDelegations: (client: FetchClient) => Promise<void>;
  readonly createDelegation: (
    request: {
      readonly worker_id: string;
      readonly worker_name: string;
      readonly namespace_mode: string;
      readonly scope_prefix?: string;
      readonly intent: string;
      readonly can_sub_delegate: boolean;
      readonly ttl_seconds?: number;
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

  // Actions — governance check
  readonly checkGovernanceEdge: (
    fromAgentId: string,
    toAgentId: string,
    zoneId: string | undefined,
    client: FetchClient,
  ) => Promise<void>;

  // Actions — UI
  readonly setActiveTab: (tab: AccessTab) => void;
  readonly setSelectedManifestIndex: (index: number) => void;
  readonly setSelectedAlertIndex: (index: number) => void;
  readonly setSelectedDisputeIndex: (index: number) => void;
  readonly setSelectedFraudIndex: (index: number) => void;
  readonly setSelectedDelegationIndex: (index: number) => void;
}

export const useAccessStore = create<AccessState>((set) => ({
  manifests: [],
  selectedManifestIndex: 0,
  manifestsLoading: false,
  lastPermissionCheck: null,
  permissionCheckLoading: false,
  alerts: [],
  alertsLoading: false,
  selectedAlertIndex: 0,
  leaderboard: [],
  leaderboardLoading: false,
  credentials: [],
  credentialsLoading: false,
  disputes: [],
  disputesLoading: false,
  selectedDisputeIndex: 0,
  fraudScores: [],
  fraudScoresLoading: false,
  selectedFraudIndex: 0,
  delegations: [],
  delegationsLoading: false,
  selectedDelegationIndex: 0,
  lastDelegationCreate: null,
  delegationChain: null,
  delegationChainLoading: false,
  governanceCheck: null,
  governanceCheckLoading: false,
  activeTab: "manifests",
  error: null,

  // ── Manifests ──────────────────────────────────────────────────────────

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

  fetchManifestDetail: async (manifestId, client) => {
    try {
      const manifest = await client.get<AccessManifest>(
        `/api/v2/access-manifests/${encodeURIComponent(manifestId)}`,
      );
      set((state) => {
        const idx = state.manifests.findIndex((m) => m.manifest_id === manifestId);
        if (idx >= 0) {
          return {
            manifests: state.manifests.map((m, i) => (i === idx ? manifest : m)),
          };
        }
        return {};
      });
    } catch {
      // Non-critical: entries just won't be available for the checker trace
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

  // ── Alerts ─────────────────────────────────────────────────────────────

  fetchAlerts: async (zoneId, client) => {
    set({ alertsLoading: true, error: null });
    try {
      const params = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : "";
      const response = await client.get<{
        readonly alerts: readonly GovernanceAlert[];
      }>(`/api/v2/governance/alerts${params}`);
      set({
        alerts: response.alerts,
        alertsLoading: false,
        selectedAlertIndex: 0,
      });
    } catch (err) {
      set({
        alertsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch alerts",
      });
    }
  },

  resolveAlert: async (alertId, resolvedBy, zoneId, client) => {
    set({ alertsLoading: true, error: null });
    try {
      const params = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : "";
      await client.post<{
        readonly alert_id: string;
        readonly resolved: boolean;
        readonly resolved_by: string;
      }>(`/api/v2/governance/alerts/${encodeURIComponent(alertId)}/resolve${params}`, {
        resolved_by: resolvedBy,
      });
      set((state) => ({
        alerts: state.alerts.map((a) =>
          a.alert_id === alertId ? { ...a, resolved: true } : a,
        ),
        alertsLoading: false,
      }));
    } catch (err) {
      set({
        alertsLoading: false,
        error: err instanceof Error ? err.message : "Failed to resolve alert",
      });
    }
  },

  // ── Reputation ─────────────────────────────────────────────────────────

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

  // ── Credentials ────────────────────────────────────────────────────────

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

  // ── Disputes ───────────────────────────────────────────────────────────

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

  // ── Fraud scores ───────────────────────────────────────────────────────

  fetchFraudScores: async (zoneId, client) => {
    set({ fraudScoresLoading: true, error: null });
    try {
      const params = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : "";
      const response = await client.get<{
        readonly scores: readonly FraudScore[];
        readonly count: number;
      }>(`/api/v2/governance/fraud-scores${params}`);
      set({
        fraudScores: response.scores,
        fraudScoresLoading: false,
        selectedFraudIndex: 0,
      });
    } catch (err) {
      set({
        fraudScoresLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch fraud scores",
      });
    }
  },

  computeFraudScores: async (zoneId, client) => {
    set({ fraudScoresLoading: true, error: null });
    try {
      const params = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : "";
      const response = await client.post<{
        readonly scores: readonly FraudScore[];
        readonly count: number;
      }>(`/api/v2/governance/fraud-scores/compute${params}`, {});
      set({
        fraudScores: response.scores,
        fraudScoresLoading: false,
        selectedFraudIndex: 0,
      });
    } catch (err) {
      set({
        fraudScoresLoading: false,
        error: err instanceof Error ? err.message : "Failed to compute fraud scores",
      });
    }
  },

  // ── UI ─────────────────────────────────────────────────────────────────

  setActiveTab: (tab) => {
    set({ activeTab: tab, error: null });
  },

  setSelectedManifestIndex: (index) => {
    set({ selectedManifestIndex: index });
  },

  setSelectedAlertIndex: (index) => {
    set({ selectedAlertIndex: index });
  },

  setSelectedDisputeIndex: (index) => {
    set({ selectedDisputeIndex: index });
  },

  setSelectedFraudIndex: (index) => {
    set({ selectedFraudIndex: index });
  },

  // ── Delegations ─────────────────────────────────────────────────────────

  fetchDelegations: async (client) => {
    set({ delegationsLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly delegations: readonly DelegationItem[];
        readonly count: number;
      }>("/api/v2/agents/delegate");
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

  createDelegation: async (request, client) => {
    set({ delegationsLoading: true, error: null });
    try {
      const response = await client.post<DelegationCreateResponse>(
        "/api/v2/agents/delegate",
        request,
      );
      set({ lastDelegationCreate: response, delegationsLoading: false });
      // Re-fetch list to include the new delegation
      const listResponse = await client.get<{
        readonly delegations: readonly DelegationItem[];
        readonly count: number;
      }>("/api/v2/agents/delegate");
      set({
        delegations: listResponse.delegations,
        selectedDelegationIndex: 0,
      });
    } catch (err) {
      set({
        delegationsLoading: false,
        error: err instanceof Error ? err.message : "Failed to create delegation",
      });
    }
  },

  revokeDelegation: async (delegationId, client) => {
    set({ delegationsLoading: true, error: null });
    try {
      await client.delete<{
        readonly status: string;
        readonly delegation_id: string;
      }>(`/api/v2/agents/delegate/${encodeURIComponent(delegationId)}`);
      set((state) => ({
        delegations: state.delegations.map((d) =>
          d.delegation_id === delegationId ? { ...d, status: "revoked" } : d,
        ),
        delegationsLoading: false,
      }));
    } catch (err) {
      set({
        delegationsLoading: false,
        error: err instanceof Error ? err.message : "Failed to revoke delegation",
      });
    }
  },

  completeDelegation: async (delegationId, outcome, qualityScore, client) => {
    set({ delegationsLoading: true, error: null });
    try {
      const body: { outcome: string; quality_score?: number } = { outcome };
      if (qualityScore !== null) {
        body.quality_score = qualityScore;
      }
      await client.post<{
        readonly status: string;
        readonly delegation_id: string;
        readonly outcome: string;
      }>(`/api/v2/agents/delegate/${encodeURIComponent(delegationId)}/complete`, body);
      set((state) => ({
        delegations: state.delegations.map((d) =>
          d.delegation_id === delegationId ? { ...d, status: outcome } : d,
        ),
        delegationsLoading: false,
      }));
    } catch (err) {
      set({
        delegationsLoading: false,
        error: err instanceof Error ? err.message : "Failed to complete delegation",
      });
    }
  },

  fetchDelegationChain: async (delegationId, client) => {
    set({ delegationChainLoading: true, error: null });
    try {
      const response = await client.get<DelegationChain>(
        `/api/v2/agents/delegate/${encodeURIComponent(delegationId)}/chain`,
      );
      set({ delegationChain: response, delegationChainLoading: false });
    } catch (err) {
      set({
        delegationChainLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch delegation chain",
      });
    }
  },

  setSelectedDelegationIndex: (index) => {
    set({ selectedDelegationIndex: index });
  },

  // ── Governance check ────────────────────────────────────────────────────

  checkGovernanceEdge: async (fromAgentId, toAgentId, zoneId, client) => {
    set({ governanceCheckLoading: true, error: null });
    try {
      const params = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : "";
      const response = await client.get<GovernanceCheckResult>(
        `/api/v2/governance/check/${encodeURIComponent(fromAgentId)}/${encodeURIComponent(toAgentId)}${params}`,
      );
      set({ governanceCheck: response, governanceCheckLoading: false });
    } catch (err) {
      set({
        governanceCheckLoading: false,
        error: err instanceof Error ? err.message : "Failed to check governance edge",
      });
    }
  },
}));
