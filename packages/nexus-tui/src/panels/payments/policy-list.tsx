/**
 * Policy list: displays spending policy records with limits and enabled status.
 */

import React from "react";
import type { PolicyRecord } from "../../stores/payments-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";

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
    return <LoadingIndicator message="Loading policies..." />;
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
        const daily = (p.daily_limit ?? "-").padEnd(11);
        const weekly = (p.weekly_limit ?? "-").padEnd(11);
        const monthly = (p.monthly_limit ?? "-").padEnd(11);
        const perTx = (p.per_tx_limit ?? "-").padEnd(11);

        return (
          <box key={p.policy_id} height={1} width="100%">
            <text>
              {`${prefix}${shortId(p.policy_id).padEnd(10)}  ${daily}  ${weekly}  ${monthly}  ${perTx}  ${enabled}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
