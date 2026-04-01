/**
 * Type definitions for the Access Control panel store.
 *
 * Extracted from access-store.ts to keep the store file focused on
 * state management and actions.
 */

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
