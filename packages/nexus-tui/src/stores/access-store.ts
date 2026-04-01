/**
 * Zustand store for the Access Control panel:
 * manifests (+ tuple entries), permission evaluation, governance alerts,
 * credentials, fraud scores.
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

/** Single step in the server-side evaluation trace (proof tree). */
export interface EvaluationTraceEntry {
  readonly index: number;
  readonly tool_pattern: string;
  readonly permission: string;
  readonly matched: boolean;
  readonly max_calls_per_minute: number | null;
}

/** Server-side evaluation trace returned by the evaluate endpoint. */
export interface EvaluationTraceResult {
  readonly matched_index: number;
  readonly default_applied: boolean;
  readonly entries: readonly EvaluationTraceEntry[];
}

export interface PermissionCheck {
  readonly tool_name: string;
  readonly permission: string;
  readonly agent_id: string;
  readonly manifest_id: string;
  readonly trace: EvaluationTraceResult | null;
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
// DelegationItem re-exported from delegation-store.ts (canonical source)

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

/** Matches backend NamespaceDetailResponse from delegation.py. */
export interface NamespaceDetail {
  readonly delegation_id: string;
  readonly agent_id: string;
  readonly delegation_mode: string;
  readonly scope_prefix: string | null;
  readonly removed_grants: readonly string[];
  readonly added_grants: readonly string[];
  readonly readonly_paths: readonly string[];
  readonly mount_table: readonly string[];
  readonly zone_id: string | null;
}

/** Matches backend governance check result from governance.py. */
export interface GovernanceCheckResult {
  readonly allowed: boolean;
  readonly constraint_type: string | null;
  readonly reason: string;
  readonly edge_id: string;
}

/** ReBAC governance constraint. */
export interface GovernanceConstraint {
  readonly id: string;
  readonly from_agent_id: string;
  readonly to_agent_id: string;
  readonly constraint_type: string;
  readonly zone_id: string;
  readonly created_at: string;
}

export type AccessTab =
  | "manifests"
  | "alerts"
  | "credentials"
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

  // Credentials
  readonly credentials: readonly Credential[];
  readonly credentialsLoading: boolean;

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

  // Namespace detail
  readonly namespaceDetail: NamespaceDetail | null;
  readonly namespaceDetailLoading: boolean;

  // Governance check
  readonly governanceCheck: GovernanceCheckResult | null;
  readonly governanceCheckLoading: boolean;

  // Collusion detection
  readonly collusionRings: readonly unknown[];
  readonly collusionLoading: boolean;

  // Governance constraints
  readonly constraints: readonly GovernanceConstraint[];
  readonly constraintsLoading: boolean;
  readonly selectedConstraintIndex: number;

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

  // Actions — credentials
  readonly fetchCredentials: (agentId: string, client: FetchClient) => Promise<void>;
  readonly issueCredential: (agentId: string, claims: Record<string, unknown>, client: FetchClient) => Promise<void>;
  readonly revokeCredential: (credentialId: string, agentId: string, client: FetchClient) => Promise<void>;

  // Actions — fraud scores
  readonly fetchFraudScores: (zoneId: string | undefined, client: FetchClient) => Promise<void>;
  readonly computeFraudScores: (zoneId: string | undefined, client: FetchClient) => Promise<void>;

  // Actions — delegations
  readonly fetchDelegations: (client: FetchClient, status?: string | null) => Promise<void>;
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

  // Actions — governance check
  readonly checkGovernanceEdge: (
    fromAgentId: string,
    toAgentId: string,
    zoneId: string | undefined,
    client: FetchClient,
  ) => Promise<void>;

  // Actions — governance constraints
  readonly fetchConstraints: (zoneId: string, client: FetchClient) => Promise<void>;
  readonly createConstraint: (constraint: { from_agent_id: string; to_agent_id: string; constraint_type: string; zone_id: string }, client: FetchClient) => Promise<void>;
  readonly deleteConstraint: (constraintId: string, client: FetchClient) => Promise<void>;
  readonly setSelectedConstraintIndex: (index: number) => void;

  // Actions — governance deep features
  readonly fetchCollusionRings: (zoneId: string | undefined, client: FetchClient) => Promise<void>;
  readonly suspendAgent: (agentId: string, reason: string, zoneId: string | undefined, client: FetchClient) => Promise<void>;

  // Actions — manifests (create/revoke)
  readonly createManifest: (
    payload: {
      readonly agent_id: string;
      readonly name: string;
      readonly entries: readonly { readonly tool_pattern: string; readonly permission: string; readonly max_calls_per_minute?: number }[];
      readonly valid_from?: string;
      readonly valid_until?: string;
    },
    client: FetchClient,
  ) => Promise<void>;
  readonly revokeManifest: (manifestId: string, client: FetchClient) => Promise<void>;

  // Actions — UI
  readonly setActiveTab: (tab: AccessTab) => void;
  readonly setSelectedManifestIndex: (index: number) => void;
  readonly setSelectedAlertIndex: (index: number) => void;
  readonly setSelectedFraudIndex: (index: number) => void;
  readonly setSelectedDelegationIndex: (index: number) => void;
}

const SOURCE = "access";

export const useAccessStore = create<AccessState>((set, get) => ({
  manifests: [],
  selectedManifestIndex: 0,
  manifestsLoading: false,
  lastPermissionCheck: null,
  permissionCheckLoading: false,
  alerts: [],
  alertsLoading: false,
  selectedAlertIndex: 0,
  credentials: [],
  credentialsLoading: false,
  fraudScores: [],
  fraudScoresLoading: false,
  selectedFraudIndex: 0,
  delegations: [],
  delegationsLoading: false,
  selectedDelegationIndex: 0,
  lastDelegationCreate: null,
  delegationChain: null,
  delegationChainLoading: false,
  namespaceDetail: null,
  namespaceDetailLoading: false,
  governanceCheck: null,
  governanceCheckLoading: false,
  collusionRings: [],
  collusionLoading: false,
  constraints: [],
  constraintsLoading: false,
  selectedConstraintIndex: 0,
  activeTab: "manifests",
  error: null,

  // =========================================================================
  // Actions migrated to createApiAction (Decision 6A)
  // =========================================================================

  // ── Manifests ──────────────────────────────────────────────────────────

  fetchManifests: createApiAction<AccessState, [FetchClient]>(set, {
    loadingKey: "manifestsLoading",
    source: SOURCE,
    action: async (client) => {
      const response = await client.get<{
        readonly manifests: readonly AccessManifest[];
        readonly offset: number;
        readonly limit: number;
        readonly count: number;
      }>("/api/v2/access-manifests");
      return {
        manifests: response.manifests,
        selectedManifestIndex: 0,
      };
    },
  }),

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

  checkPermission: createApiAction<AccessState, [string, string, FetchClient]>(set, {
    loadingKey: "permissionCheckLoading",
    source: SOURCE,
    action: async (manifestId, toolName, client) => {
      const response = await client.post<{
        readonly tool_name: string;
        readonly permission: string;
        readonly agent_id: string;
        readonly manifest_id: string;
        readonly trace?: EvaluationTraceResult;
      }>(`/api/v2/access-manifests/${encodeURIComponent(manifestId)}/evaluate`, {
        tool_name: toolName,
      });
      return {
        lastPermissionCheck: {
          tool_name: response.tool_name,
          permission: response.permission,
          agent_id: response.agent_id,
          manifest_id: response.manifest_id,
          trace: response.trace ?? null,
        },
      };
    },
  }),

  // ── Alerts ─────────────────────────────────────────────────────────────

  fetchAlerts: createApiAction<AccessState, [string | undefined, FetchClient]>(set, {
    loadingKey: "alertsLoading",
    source: SOURCE,
    action: async (zoneId, client) => {
      const params = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : "";
      const response = await client.get<{
        readonly alerts: readonly GovernanceAlert[];
      }>(`/api/v2/governance/alerts${params}`);
      return {
        alerts: response.alerts,
        selectedAlertIndex: 0,
      };
    },
  }),

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
      const message = err instanceof Error ? err.message : "Failed to resolve alert";
      set({ alertsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  // ── Credentials ────────────────────────────────────────────────────────

  fetchCredentials: createApiAction<AccessState, [string, FetchClient]>(set, {
    loadingKey: "credentialsLoading",
    source: SOURCE,
    action: async (agentId, client) => {
      const response = await client.get<{
        readonly agent_id: string;
        readonly count: number;
        readonly credentials: readonly Credential[];
      }>(`/api/v2/agents/${encodeURIComponent(agentId)}/credentials`);
      return { credentials: response.credentials };
    },
  }),

  issueCredential: async (agentId, claims, client) => {
    set({ credentialsLoading: true, error: null });
    try {
      await client.post(`/api/v2/agents/${encodeURIComponent(agentId)}/credentials`, { claims });
      await get().fetchCredentials(agentId, client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to issue credential";
      set({ credentialsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  revokeCredential: async (credentialId, agentId, client) => {
    set({ credentialsLoading: true, error: null });
    try {
      await client.post(`/api/v2/agents/${encodeURIComponent(agentId)}/credentials/${encodeURIComponent(credentialId)}/revoke`, {});
      set((state) => ({
        credentials: state.credentials.map((c) =>
          c.credential_id === credentialId ? { ...c, is_active: false, revoked_at: new Date().toISOString() } : c,
        ),
        credentialsLoading: false,
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to revoke credential";
      set({ credentialsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  // ── Fraud scores ───────────────────────────────────────────────────────

  fetchFraudScores: createApiAction<AccessState, [string | undefined, FetchClient]>(set, {
    loadingKey: "fraudScoresLoading",
    source: SOURCE,
    action: async (zoneId, client) => {
      const params = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : "";
      const response = await client.get<{
        readonly scores: readonly FraudScore[];
        readonly count: number;
      }>(`/api/v2/governance/fraud-scores${params}`);
      return {
        fraudScores: response.scores,
        selectedFraudIndex: 0,
      };
    },
  }),

  computeFraudScores: createApiAction<AccessState, [string | undefined, FetchClient]>(set, {
    loadingKey: "fraudScoresLoading",
    source: SOURCE,
    action: async (zoneId, client) => {
      const params = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : "";
      const response = await client.post<{
        readonly scores: readonly FraudScore[];
        readonly count: number;
      }>(`/api/v2/governance/fraud-scores/compute${params}`, {});
      return {
        fraudScores: response.scores,
        selectedFraudIndex: 0,
      };
    },
  }),

  // ── Manifests (create/revoke) ─────────────────────────────────────────

  createManifest: async (payload, client) => {
    set({ manifestsLoading: true, error: null });
    try {
      await client.post<AccessManifest>("/api/v2/access-manifests", payload);
      // Re-fetch manifest list
      const response = await client.get<{ manifests: readonly AccessManifest[]; }>("/api/v2/access-manifests");
      set({ manifests: response.manifests, manifestsLoading: false, selectedManifestIndex: 0 });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to create manifest";
      set({ manifestsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  revokeManifest: async (manifestId, client) => {
    set({ manifestsLoading: true, error: null });
    try {
      await client.post(`/api/v2/access-manifests/${encodeURIComponent(manifestId)}/revoke`, {});
      set((state) => ({
        manifests: state.manifests.map((m) =>
          m.manifest_id === manifestId ? { ...m, status: "revoked" } : m,
        ),
        manifestsLoading: false,
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to revoke manifest";
      set({ manifestsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
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

  setSelectedFraudIndex: (index) => {
    set({ selectedFraudIndex: index });
  },

  // ── Delegations ─────────────────────────────────────────────────────────

  fetchDelegations: async (client, status) => {
    set({ delegationsLoading: true, error: null });
    try {
      let url = "/api/v2/agents/delegate";
      if (status) url += `?status=${encodeURIComponent(status)}`;
      const response = await client.get<{
        readonly delegations: readonly DelegationItem[];
        readonly count: number;
      }>(url);
      set({
        delegations: response.delegations,
        delegationsLoading: false,
        selectedDelegationIndex: 0,
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
    // Re-fetch list separately — a GET failure here must not mask the successful POST
    try {
      const listResponse = await client.get<{
        readonly delegations: readonly DelegationItem[];
        readonly count: number;
      }>("/api/v2/agents/delegate");
      set({
        delegations: listResponse.delegations,
        selectedDelegationIndex: 0,
      });
    } catch {
      // Non-critical: list will refresh on next tab visit
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
      const message = err instanceof Error ? err.message : "Failed to revoke delegation";
      set({ delegationsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
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

  fetchDelegationChain: createApiAction<AccessState, [string, FetchClient]>(set, {
    loadingKey: "delegationChainLoading",
    source: SOURCE,
    action: async (delegationId, client) => {
      const response = await client.get<DelegationChain>(
        `/api/v2/agents/delegate/${encodeURIComponent(delegationId)}/chain`,
      );
      return { delegationChain: response };
    },
  }),

  fetchNamespaceDetail: createApiAction<AccessState, [string, FetchClient]>(set, {
    loadingKey: "namespaceDetailLoading",
    source: SOURCE,
    action: async (delegationId, client) => {
      const response = await client.get<NamespaceDetail>(
        `/api/v2/agents/delegate/${encodeURIComponent(delegationId)}/namespace`,
      );
      return { namespaceDetail: response };
    },
  }),

  updateNamespaceConfig: createApiAction<AccessState, [string, { readonly scope_prefix?: string; readonly remove_grants?: readonly string[]; readonly add_grants?: readonly string[]; readonly readonly_paths?: readonly string[] }, FetchClient]>(set, {
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

  // ── Governance check ────────────────────────────────────────────────────

  checkGovernanceEdge: createApiAction<AccessState, [string, string, string | undefined, FetchClient]>(set, {
    loadingKey: "governanceCheckLoading",
    source: SOURCE,
    action: async (fromAgentId, toAgentId, zoneId, client) => {
      const params = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : "";
      const response = await client.get<GovernanceCheckResult>(
        `/api/v2/governance/check/${encodeURIComponent(fromAgentId)}/${encodeURIComponent(toAgentId)}${params}`,
      );
      return { governanceCheck: response };
    },
  }),

  // ── Governance constraints ─────────────────────────────────────────────

  fetchConstraints: createApiAction<AccessState, [string, FetchClient]>(set, {
    loadingKey: "constraintsLoading",
    source: SOURCE,
    action: async (zoneId, client) => {
      const response = await client.get<{
        readonly constraints: readonly GovernanceConstraint[];
      }>(`/api/v2/governance/constraints?zone_id=${encodeURIComponent(zoneId)}`);
      return {
        constraints: response.constraints,
        selectedConstraintIndex: 0,
      };
    },
  }),

  createConstraint: async (constraint, client) => {
    set({ constraintsLoading: true, error: null });
    try {
      await client.post("/api/v2/governance/constraints", constraint);
      await get().fetchConstraints(constraint.zone_id, client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to create constraint";
      set({ constraintsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  deleteConstraint: async (constraintId, client) => {
    set({ constraintsLoading: true, error: null });
    try {
      await client.delete(`/api/v2/governance/constraints/${encodeURIComponent(constraintId)}`);
      set((state) => ({
        constraints: state.constraints.filter((c) => c.id !== constraintId),
        constraintsLoading: false,
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to delete constraint";
      set({ constraintsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  setSelectedConstraintIndex: (index) => {
    set({ selectedConstraintIndex: index });
  },

  // ── Governance deep features ───────────────────────────────────────────

  fetchCollusionRings: createApiAction<AccessState, [string | undefined, FetchClient]>(set, {
    loadingKey: "collusionLoading",
    source: SOURCE,
    action: async (zoneId, client) => {
      const params = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : "";
      const response = await client.get<{ rings: readonly unknown[] }>(
        `/api/v2/governance/collusion-rings${params}`,
      );
      return { collusionRings: response.rings ?? [] };
    },
  }),

  suspendAgent: async (agentId, reason, zoneId, client) => {
    set({ error: null });
    try {
      const params = zoneId ? `?zone_id=${encodeURIComponent(zoneId)}` : "";
      await client.post(`/api/v2/governance/suspend/${encodeURIComponent(agentId)}${params}`, { reason });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to suspend agent";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },
}));
