// Generated from Nexus product_agent_registry.py. DO NOT EDIT.
export const AGENT_REGISTRY_API = "agent-registry.sudo.dev/v1alpha1" as const
/** Runtime validation uses the existing ResourceRef owner schema and adapter. */
export type ResourceReference = Record<string, unknown>
export type Channel = { name: "stable" | "canary"; version: string; spec_digest: string; revision: number; enabled: boolean; updated_at: number; }
export type CommandResult = { api_version?: "agent-registry.sudo.dev/v1alpha1"; kind?: "RegistryCommandResult"; principal: Principal; version?: (SignedVersion | null); decision?: (Decision | null); channel?: (Channel | null); }
export type DecideVersion = { api_version: "agent-registry.sudo.dev/v1alpha1"; request_id: string; principal: PrincipalRef; kind: "DecideVersion"; version: string; decision: "approved" | "withdrawn" | "revoked"; reason: string; expires_at?: (number | null); }
export type Decision = { id: string; version: string; decision: "approved" | "withdrawn" | "revoked"; actor: string; reason: string; expires_at: (number | null); created_at: number; registry_revision: number; }
export type MoveChannel = { api_version: "agent-registry.sudo.dev/v1alpha1"; request_id: string; principal: PrincipalRef; kind: "MoveChannel"; channel: "stable" | "canary"; version: string; expected_revision: number; reason: string; }
export type Principal = { principal: PrincipalRef; owner_zone: string; display_name: string; status: "active" | "suspended" | "revoked"; revision: number; created_at: number; }
export type PrincipalRef = { trust_domain: string; agent_name: string; }
export type PublishVersion = { api_version: "agent-registry.sudo.dev/v1alpha1"; request_id: string; principal: PrincipalRef; kind: "PublishVersion"; version: string; spec_ref: ResourceReference; spec_digest: string; source_evidence_refs?: Array<ResourceReference>; }
export type RegisterPrincipal = { api_version: "agent-registry.sudo.dev/v1alpha1"; request_id: string; principal: PrincipalRef; kind: "RegisterPrincipal"; display_name: string; }
export type Resolution = { api_version?: "agent-registry.sudo.dev/v1alpha1"; kind?: "RegistryResolution"; principal: Principal; version: SignedVersion; approval: Decision; channel: (Channel | null); evaluated_at: number; }
export type SetPrincipalStatus = { api_version: "agent-registry.sudo.dev/v1alpha1"; request_id: string; principal: PrincipalRef; kind: "SetPrincipalStatus"; status: "active" | "suspended" | "revoked"; expected_revision: number; reason: string; }
export type SignedVersion = { manifest: VersionManifest; payload_base64: string; digest: string; algorithm?: "Ed25519"; key_id: string; signature_base64: string; }
export type VersionItem = { version: SignedVersion; decision: (Decision | null); }
export type VersionManifest = { api_version?: "agent-registry.sudo.dev/v1alpha1"; kind?: "VersionManifest"; principal: PrincipalRef; version: string; spec_ref: ResourceReference; spec_digest: string; source_evidence_refs: Array<ResourceReference>; created_by: string; created_at: number; evidence?: "signed_registration_only"; }
export type VersionsPage = { api_version?: "agent-registry.sudo.dev/v1alpha1"; kind?: "RegistryVersionsPage"; principal: Principal; items: Array<VersionItem>; channels: Array<Channel>; next_cursor: (string | null); }
export type RegistryWire = ((RegisterPrincipal | PublishVersion | DecideVersion | MoveChannel | SetPrincipalStatus) | CommandResult | Resolution | VersionsPage | VersionItem)
