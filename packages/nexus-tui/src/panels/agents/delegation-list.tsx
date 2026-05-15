/**
 * Delegation list table with status badges and expandable detail view.
 */

import { createEffect, createSignal } from "solid-js";
import type { JSX } from "solid-js";
import type { DelegationItem } from "../../stores/agents-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { delegationModeColor, delegationStatusColor, statusColor } from "../../shared/theme.js";
import { textStyle } from "../../shared/text-style.js";

interface DelegationListProps {
  readonly delegations: readonly DelegationItem[];
  readonly selectedIndex: number;
  readonly loading: boolean;
  readonly expandedDelegation?: DelegationItem | null;
}

const STATUS_BADGES: Readonly<Record<DelegationItem["status"], string>> = {
  active: "●",
  revoked: "✗",
  expired: "○",
  completed: "✓",
};

interface PermTuple {
  readonly relation: string;
  readonly object_type: string;
  readonly object_id: string;
}

function DelegationDetail({ delegation }: { delegation: DelegationItem }): JSX.Element {
  const client = useApi();
  const [perms, setPerms] = createSignal<readonly PermTuple[]>([]);

  createEffect(() => {
    if (!client) return;
    client.get<{ permissions: PermTuple[] }>(
      `/api/v2/agents/${encodeURIComponent(delegation.agent_id)}/permissions`,
    ).then((r) => setPerms(r.permissions))
      .catch(() => setPerms([]));
  });

  return (
    <box height={11 + Math.max(perms.length, 1)} width="100%" borderStyle="single" flexDirection="column">
      <text>{"Delegation Detail (Esc to close)"}</text>
      <text>{`  ID:       ${delegation.delegation_id}`}</text>
      <text>{`  Worker:   ${delegation.agent_id}  →  Parent: ${delegation.parent_agent_id}`}</text>
      <text>{`  Mode:     ${delegation.delegation_mode}   Status: ${delegation.status}   Depth: ${delegation.depth}   Sub-delegate: ${delegation.can_sub_delegate ? "yes" : "no"}`}</text>
      <text>{`  Intent:   ${delegation.intent}`}</text>
      <text>{`  Scope:    ${delegation.scope_prefix ?? "(none)"}   Zone: ${delegation.zone_id ?? "(none)"}`}</text>
      <text>{`  Created:  ${delegation.created_at}`}</text>
      <text>{`  Expires:  ${formatExpiry(delegation.lease_expires_at)}`}</text>
      <text>{""}</text>
      <text style={textStyle({ fg: "cyan", bold: true })}>{"  Granted Capabilities:"}</text>
      {perms.length === 0 ? (
        <text style={textStyle({ dim: true })}>{"    (none or loading...)"}</text>
      ) : (
        perms().map((p, i) => {
          const tool = p.object_id.replace("/tools/", "");
          const accessLevel = p.relation.replace("direct_", "");
          const icon = accessLevel === "viewer" || accessLevel === "reader" ? "R" : accessLevel === "editor" || accessLevel === "writer" ? "W" : "?";
          const color = icon === "R" ? "cyan" : icon === "W" ? "yellow" : "gray";
          return (
            <text key={`perm-${i}`}>
              <span style={textStyle({ fg: color })}>{`    [${icon}] `}</span>
              <span>{tool}</span>
              <span style={textStyle({ dim: true })}>{` (${accessLevel})`}</span>
            </text>
          );
        })
      )}
    </box>
  );
}

function shortId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 8)}..`;
}

function formatExpiry(ts: string | null): string {
  if (!ts) return "never";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function DelegationList(props: DelegationListProps): JSX.Element {
  // Unconditional rendering — avoid if/return which evaluates once in Match branches.
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>{props.loading ? "Loading delegations..." : props.delegations.length === 0 ? "No delegations found" : ""}</text>
      <scrollbox flexGrow={props.expandedDelegation ? 0 : 1} width="100%">
        {/* Header */}
        <box height={1} width="100%">
          <text>{"  ST  ID          MODE    AGENT->PARENT        INTENT               DEPTH  EXPIRES"}</text>
        </box>
        <box height={1} width="100%">
          <text>{"  --  ----------  ------  -------------------  -------------------  -----  -------"}</text>
        </box>

        {/* Rows */}
        {props.delegations.map((d, i) => {
          const isSelected = i === props.selectedIndex;
          const badge = STATUS_BADGES[d.status] ?? "?";
          const badgeColor = delegationStatusColor[d.status] ?? statusColor.dim;
          const modeColor = delegationModeColor[d.delegation_mode] ?? statusColor.dim;
          const prefix = isSelected ? "> " : "  ";

          return (
            <box key={d.delegation_id} height={1} width="100%">
              <text>
                <span>{prefix}</span>
                <span style={textStyle({ fg: badgeColor })}>{badge}</span>
                <span style={textStyle({ dim: true })}>{`   ${shortId(d.delegation_id).padEnd(10)}  `}</span>
                <span style={textStyle({ fg: modeColor })}>{d.delegation_mode.padEnd(6)}</span>
                <span>{"  "}</span>
                <span style={textStyle({ fg: statusColor.identity })}>{shortId(d.agent_id)}</span>
                <span style={textStyle({ dim: true })}>{"→"}</span>
                <span>{shortId(d.parent_agent_id).padEnd(12)}</span>
                <span style={textStyle({ dim: true })}>{"  "}</span>
                <span>{(d.intent.length > 19 ? `${d.intent.slice(0, 16)}...` : d.intent).padEnd(19)}</span>
                <span style={textStyle({ dim: true })}>{`  ${String(d.depth).padEnd(5)}  ${formatExpiry(d.lease_expires_at)}`}</span>
              </text>
            </box>
          );
        })}
      </scrollbox>

      {/* Expanded delegation detail */}
      {props.expandedDelegation && (
        <DelegationDetail delegation={props.expandedDelegation!} />
      )}
    </box>
  );
}
