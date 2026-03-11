/**
 * Policy list: displays spending policy records with limits and enabled status.
 */

import React from "react";
import type { PolicyRecord } from "../../stores/payments-store.js";

interface PolicyListProps {
  readonly policies: readonly PolicyRecord[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function shortId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 8)}..`;
}

export function PolicyList({
  policies,
  selectedIndex,
  loading,
}: PolicyListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading policies...</text>
      </box>
    );
  }

  if (policies.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No spending policies found</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  ID          DAILY        WEEKLY       MONTHLY      PER-TX       ENABLED"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ----------  -----------  -----------  -----------  -----------  -------"}</text>
      </box>

      {/* Rows */}
      {policies.map((p, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const enabled = p.enabled ? "yes" : "no";

        return (
          <box key={p.policy_id} height={1} width="100%">
            <text>
              {`${prefix}${shortId(p.policy_id).padEnd(10)}  ${p.daily_limit.padEnd(11)}  ${p.weekly_limit.padEnd(11)}  ${p.monthly_limit.padEnd(11)}  ${p.per_tx_limit.padEnd(11)}  ${enabled}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
