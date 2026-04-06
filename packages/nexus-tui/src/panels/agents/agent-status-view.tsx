import { Show } from "solid-js";
import type { JSX } from "solid-js";
/**
 * Agent status detail view: phase badge, conditions, resource usage, identity.
 */

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

function truncateHex(hex: string | null | undefined, len = 16): string {
  if (!hex) return "n/a";
  return hex.length > len ? hex.slice(0, len) + "..." : hex;
}

export function AgentStatusView(props: AgentStatusViewProps): JSX.Element {
  return (
    <Show when={!props.loading} fallback={<LoadingIndicator message="Loading agent status..." />}>
      <Show when={props.status} fallback={
        <box height="100%" width="100%" justifyContent="center" alignItems="center">
          <text>Select an agent to view status</text>
        </box>
      }>
        {(status) => {
          const badge = PHASE_BADGES[status().phase] ?? `[${status().phase.toUpperCase()}]`;
          return (
            <scrollbox height="100%" width="100%">
              <box height={1} width="100%">
                <text>{`Phase: ${badge} ${status().phase}  |  Generation: ${status().observed_generation}`}</text>
              </box>
              <box height={1} width="100%">
                <text>{`Last heartbeat: ${formatTimestamp(status().last_heartbeat)}`}</text>
              </box>
              <box height={1} width="100%">
                <text>{`Last activity:  ${formatTimestamp(status().last_activity)}`}</text>
              </box>
              <box height={1} width="100%">
                <text>{`Inbox depth: ${status().inbox_depth}  |  Context usage: ${status().context_usage_pct}%`}</text>
              </box>
              <box height={1} width="100%" marginTop={1}>
                <text>--- Resource Usage ---</text>
              </box>
              <box height={1} width="100%">
                <text>{`Tokens used:    ${status().resource_usage.tokens_used}`}</text>
              </box>
              <box height={1} width="100%">
                <text>{`Storage:        ${status().resource_usage.storage_used_mb} MB`}</text>
              </box>
              <box height={1} width="100%">
                <text>{`Context:        ${renderUsageBar(status().resource_usage.context_usage_pct, 20)}`}</text>
              </box>

              {status().conditions.length > 0 && (
                <>
                  <box height={1} width="100%" marginTop={1}>
                    <text>--- Conditions ---</text>
                  </box>
                  {status().conditions.map((cond, i) => (
                    <box key={`cond-${i}`} height={1} width="100%">
                      <text>{`[${cond.status}] ${cond.type}: ${cond.reason} - ${cond.message}`}</text>
                    </box>
                  ))}
                </>
              )}

              {props.spec && (
                <>
                  <box height={1} width="100%" marginTop={1}>
                    <text>--- Spec ---</text>
                  </box>
                  <box height={1} width="100%">
                    <text>{`Type: ${props.spec.agent_type}  |  QoS: ${props.spec.qos_class}  |  Gen: ${props.spec.spec_generation}`}</text>
                  </box>
                  {props.spec.zone_affinity && (
                    <box height={1} width="100%">
                      <text>{`Zone affinity: ${props.spec.zone_affinity}`}</text>
                    </box>
                  )}
                  {props.spec.capabilities.length > 0 && (
                    <box height={1} width="100%">
                      <text>{`Capabilities: ${props.spec.capabilities.join(", ")}`}</text>
                    </box>
                  )}
                </>
              )}

              {props.identity && (
                <>
                  <box height={1} width="100%" marginTop={1}>
                    <text>--- Identity ---</text>
                  </box>
                  <box height={1} width="100%">
                    <text>{`DID: ${props.identity.did}`}</text>
                  </box>
                  <box height={1} width="100%">
                    <text>{`Key ID: ${props.identity.key_id}`}</text>
                  </box>
                  <box height={1} width="100%">
                    <text>{`Algorithm: ${props.identity.algorithm}`}</text>
                  </box>
                  <box height={1} width="100%">
                    <text>{`Public key: ${truncateHex(props.identity.public_key_hex)}`}</text>
                  </box>
                  {props.identity.created_at && (
                    <box height={1} width="100%">
                      <text>{`Created: ${formatTimestamp(props.identity.created_at)}`}</text>
                    </box>
                  )}
                  {props.identity.expires_at && (
                    <box height={1} width="100%">
                      <text>{`Expires: ${formatTimestamp(props.identity.expires_at)}`}</text>
                    </box>
                  )}
                </>
              )}

              {props.trustScore != null && (
                <>
                  <box height={1} width="100%" marginTop={1}>
                    <text>--- Trust ---</text>
                  </box>
                  <box height={1} width="100%">
                    <text>{`Trust score: ${props.trustScore.toFixed(2)} ${renderUsageBar(props.trustScore * 100, 20)}`}</text>
                  </box>
                </>
              )}

              {props.reputation != null && (
                <>
                  <box height={1} width="100%" marginTop={1}>
                    <text>--- Reputation ---</text>
                  </box>
                  <box height={1} width="100%">
                    <text>{`${JSON.stringify(props.reputation, null, 2)}`}</text>
                  </box>
                </>
              )}
            </scrollbox>
          );
        }}
      </Show>
    </Show>
  );
}
