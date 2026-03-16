/**
 * Agent status detail view: phase badge, conditions, resource usage, identity.
 */

import React from "react";
import type { AgentStatus, AgentSpec, AgentIdentity, AgentPhase } from "../../stores/agents-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";

interface AgentStatusViewProps {
  readonly status: AgentStatus | null;
  readonly spec: AgentSpec | null;
  readonly identity: AgentIdentity | null;
  readonly loading: boolean;
  readonly trustScore?: number | null;
  readonly reputation?: unknown | null;
}

const PHASE_BADGES: Readonly<Record<AgentPhase, string>> = {
  warming: "[WRM]",
  ready: "[RDY]",
  active: "[ACT]",
  thinking: "[THK]",
  idle: "[IDL]",
  suspended: "[SUS]",
  evicted: "[EVT]",
};

function formatTimestamp(ts: string | null): string {
  if (!ts) return "n/a";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function renderUsageBar(pct: number, width: number): string {
  const filled = Math.round((pct / 100) * width);
  const empty = width - filled;
  return `[${"#".repeat(filled)}${"-".repeat(empty)}] ${pct.toFixed(0)}%`;
}

export function AgentStatusView({
  status,
  spec,
  identity,
  loading,
  trustScore,
  reputation,
}: AgentStatusViewProps): React.ReactNode {
  if (loading) {
    return <LoadingIndicator message="Loading agent status..." />;
  }

  if (!status) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Select an agent to view status</text>
      </box>
    );
  }

  const badge = PHASE_BADGES[status.phase] ?? `[${status.phase.toUpperCase()}]`;

  return (
    <scrollbox height="100%" width="100%">
      {/* Phase and generation */}
      <box height={1} width="100%">
        <text>{`Phase: ${badge} ${status.phase}  |  Generation: ${status.observed_generation}`}</text>
      </box>

      {/* Timestamps */}
      <box height={1} width="100%">
        <text>{`Last heartbeat: ${formatTimestamp(status.last_heartbeat)}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Last activity:  ${formatTimestamp(status.last_activity)}`}</text>
      </box>

      {/* Inbox and context */}
      <box height={1} width="100%">
        <text>{`Inbox depth: ${status.inbox_depth}  |  Context usage: ${status.context_usage_pct}%`}</text>
      </box>

      {/* Resource usage */}
      <box height={1} width="100%" marginTop={1}>
        <text>--- Resource Usage ---</text>
      </box>
      <box height={1} width="100%">
        <text>{`Tokens used:    ${status.resource_usage.tokens_used}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Storage:        ${status.resource_usage.storage_used_mb} MB`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Context:        ${renderUsageBar(status.resource_usage.context_usage_pct, 20)}`}</text>
      </box>

      {/* Conditions */}
      {status.conditions.length > 0 && (
        <>
          <box height={1} width="100%" marginTop={1}>
            <text>--- Conditions ---</text>
          </box>
          {status.conditions.map((cond, i) => (
            <box key={`cond-${i}`} height={1} width="100%">
              <text>{`[${cond.status}] ${cond.type}: ${cond.reason} - ${cond.message}`}</text>
            </box>
          ))}
        </>
      )}

      {/* Spec info */}
      {spec && (
        <>
          <box height={1} width="100%" marginTop={1}>
            <text>--- Spec ---</text>
          </box>
          <box height={1} width="100%">
            <text>{`Type: ${spec.agent_type}  |  QoS: ${spec.qos_class}  |  Gen: ${spec.spec_generation}`}</text>
          </box>
          {spec.zone_affinity && (
            <box height={1} width="100%">
              <text>{`Zone affinity: ${spec.zone_affinity}`}</text>
            </box>
          )}
          {spec.capabilities.length > 0 && (
            <box height={1} width="100%">
              <text>{`Capabilities: ${spec.capabilities.join(", ")}`}</text>
            </box>
          )}
        </>
      )}

      {/* Identity */}
      {identity && (
        <>
          <box height={1} width="100%" marginTop={1}>
            <text>--- Identity ---</text>
          </box>
          <box height={1} width="100%">
            <text>{`DID: ${identity.did}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`Key ID: ${identity.key_id}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`Algorithm: ${identity.algorithm}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`Public key: ${truncateHex(identity.public_key_hex)}`}</text>
          </box>
          {identity.created_at && (
            <box height={1} width="100%">
              <text>{`Created: ${formatTimestamp(identity.created_at)}`}</text>
            </box>
          )}
          {identity.expires_at && (
            <box height={1} width="100%">
              <text>{`Expires: ${formatTimestamp(identity.expires_at)}`}</text>
            </box>
          )}
        </>
      )}

      {/* Trust Score */}
      {trustScore != null && (
        <>
          <box height={1} width="100%" marginTop={1}>
            <text>--- Trust ---</text>
          </box>
          <box height={1} width="100%">
            <text>{`Trust score: ${trustScore.toFixed(2)} ${renderUsageBar(trustScore * 100, 20)}`}</text>
          </box>
        </>
      )}

      {/* Reputation */}
      {reputation != null && (
        <>
          <box height={1} width="100%" marginTop={1}>
            <text>--- Reputation ---</text>
          </box>
          <box height={1} width="100%">
            <text>{`${JSON.stringify(reputation, null, 2)}`}</text>
          </box>
        </>
      )}
    </scrollbox>
  );
}

function truncateHex(hex: string): string {
  if (hex.length <= 20) return hex;
  return `${hex.slice(0, 10)}...${hex.slice(-10)}`;
}
