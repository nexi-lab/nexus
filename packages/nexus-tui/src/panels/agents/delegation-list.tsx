/**
 * Delegation list table with status badges and expandable detail view.
 */

import React, { useEffect, useState } from "react";
import type { DelegationItem } from "../../stores/agents-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { delegationModeColor, delegationStatusColor, statusColor } from "../../shared/theme.js";

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

function DelegationDetail({ delegation }: { delegation: DelegationItem }): React.ReactNode {
  const client = useApi();
  const [perms, setPerms] = useState<readonly PermTuple[]>([]);

  useEffect(() => {
    if (!client) return;
    client.get<{ permissions: PermTuple[] }>(
      `/api/v2/agents/${encodeURIComponent(delegation.agent_id)}/permissions`,
    ).then((r) => setPerms(r.permissions))
      .catch(() => setPerms([]));
  }, [client, delegation.agent_id]);

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
      <text bold foregroundColor="cyan">{"  Granted Permissions:"}</text>
      {perms.length === 0 ? (
        <text dimColor>{"    (none or loading...)"}</text>
      ) : (
        perms.map((p, i) => (
          <text key={`perm-${i}`}>
            <span foregroundColor="green">{`    ${p.relation}`}</span>
            <span dimColor>{" on "}</span>
            <span foregroundColor="blue">{`${p.object_type}:${p.object_id}`}</span>
          </text>
        ))
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

export function DelegationList({
  delegations,
  selectedIndex,
  loading,
  expandedDelegation,
}: DelegationListProps): React.ReactNode {
  if (loading) {
    return <LoadingIndicator message="Loading delegations..." />;
  }

  if (delegations.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No delegations found</text>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      <scrollbox flexGrow={expandedDelegation ? 0 : 1} width="100%">
        {/* Header */}
        <box height={1} width="100%">
          <text>{"  ST  ID          MODE    AGENT->PARENT        INTENT               DEPTH  EXPIRES"}</text>
        </box>
        <box height={1} width="100%">
          <text>{"  --  ----------  ------  -------------------  -------------------  -----  -------"}</text>
        </box>

        {/* Rows */}
        {delegations.map((d, i) => {
          const isSelected = i === selectedIndex;
          const badge = STATUS_BADGES[d.status] ?? "?";
          const badgeColor = delegationStatusColor[d.status] ?? statusColor.dim;
          const modeColor = delegationModeColor[d.delegation_mode] ?? statusColor.dim;
          const prefix = isSelected ? "> " : "  ";

          return (
            <box key={d.delegation_id} height={1} width="100%">
              <text>
                <span>{prefix}</span>
                <span foregroundColor={badgeColor}>{badge}</span>
                <span dimColor>{`   ${shortId(d.delegation_id).padEnd(10)}  `}</span>
                <span foregroundColor={modeColor}>{d.delegation_mode.padEnd(6)}</span>
                <span>{"  "}</span>
                <span foregroundColor={statusColor.identity}>{shortId(d.agent_id)}</span>
                <span dimColor>{"→"}</span>
                <span>{shortId(d.parent_agent_id).padEnd(12)}</span>
                <span dimColor>{"  "}</span>
                <span>{(d.intent.length > 19 ? `${d.intent.slice(0, 16)}...` : d.intent).padEnd(19)}</span>
                <span dimColor>{`  ${String(d.depth).padEnd(5)}  ${formatExpiry(d.lease_expires_at)}`}</span>
              </text>
            </box>
          );
        })}
      </scrollbox>

      {/* Expanded delegation detail */}
      {expandedDelegation && (
        <DelegationDetail delegation={expandedDelegation} />
      )}
    </box>
  );
}
