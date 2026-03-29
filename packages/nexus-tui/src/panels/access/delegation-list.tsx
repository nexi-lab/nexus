/**
 * Delegation list: shows delegations with scope_prefix as namespace view,
 * agent hierarchy, mode, status, and lease expiry.
 */

import React from "react";
import type { DelegationItem } from "../../stores/access-store.js";
import { EmptyState } from "../../shared/components/empty-state.js";

interface DelegationListProps {
  readonly delegations: readonly DelegationItem[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function shortId(id: string): string {
  if (id.length <= 14) return id;
  return `${id.slice(0, 11)}..`;
}

function formatExpiry(ts: string | null): string {
  if (!ts) return "-";
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
}: DelegationListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading delegations...</text>
      </box>
    );
  }

  if (delegations.length === 0) {
    return <EmptyState message="No delegations yet." hint="Press n to create one." />;
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  AGENT          PARENT         SCOPE PREFIX         MODE       STATUS     DEPTH  SUB-DEL  LEASE EXPIRES"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  -------------  -------------  -------------------  ---------  ---------  -----  -------  -----------------"}</text>
      </box>

      {/* Rows */}
      {delegations.map((d, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const scope = d.scope_prefix ?? "*";
        const subDel = d.can_sub_delegate ? "yes" : "no";

        return (
          <box key={d.delegation_id} height={1} width="100%">
            <text>
              {`${prefix}${shortId(d.agent_id).padEnd(13)}  ${shortId(d.parent_agent_id).padEnd(13)}  ${scope.padEnd(19)}  ${d.delegation_mode.padEnd(9)}  ${d.status.padEnd(9)}  ${String(d.depth).padEnd(5)}  ${subDel.padEnd(7)}  ${formatExpiry(d.lease_expires_at)}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
