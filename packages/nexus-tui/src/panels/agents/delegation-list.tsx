/**
 * Delegation list table with status badges and expandable detail view.
 */

import React from "react";
import type { DelegationItem } from "../../stores/agents-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";

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
          const arrow = `${shortId(d.agent_id)}->${shortId(d.parent_agent_id)}`;
          const intent = d.intent.length > 19 ? `${d.intent.slice(0, 16)}...` : d.intent;
          const prefix = isSelected ? "> " : "  ";

          return (
            <box key={d.delegation_id} height={1} width="100%">
              <text>
                {`${prefix}${badge}   ${shortId(d.delegation_id).padEnd(10)}  ${d.delegation_mode.padEnd(6)}  ${arrow.padEnd(19)}  ${intent.padEnd(19)}  ${String(d.depth).padEnd(5)}  ${formatExpiry(d.lease_expires_at)}`}
              </text>
            </box>
          );
        })}
      </scrollbox>

      {/* Expanded delegation detail */}
      {expandedDelegation && (
        <box height={9} width="100%" borderStyle="single" flexDirection="column">
          <box height={1} width="100%">
            <text>{"--- Delegation Detail (Esc to close) ---"}</text>
          </box>
          <box height={1} width="100%">
            <text>{`  ID:          ${expandedDelegation.delegation_id}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`  Agent:       ${expandedDelegation.agent_id}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`  Parent:      ${expandedDelegation.parent_agent_id}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`  Mode:        ${expandedDelegation.delegation_mode}  Status: ${expandedDelegation.status}  Depth: ${expandedDelegation.depth}  Sub-delegate: ${expandedDelegation.can_sub_delegate ? "yes" : "no"}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`  Intent:      ${expandedDelegation.intent}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`  Scope:       ${expandedDelegation.scope_prefix ?? "(none)"}  Zone: ${expandedDelegation.zone_id ?? "(none)"}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`  Created:     ${expandedDelegation.created_at}  Expires: ${formatExpiry(expandedDelegation.lease_expires_at)}`}</text>
          </box>
        </box>
      )}
    </box>
  );
}
