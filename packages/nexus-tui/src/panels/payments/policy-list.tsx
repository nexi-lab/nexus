/**
 * Payment policy list: name, type, limit, and enabled status.
 */

import React from "react";
import type { PaymentPolicy } from "../../stores/payments-store.js";

interface PolicyListProps {
  readonly policies: readonly PaymentPolicy[];
  readonly loading: boolean;
}

function shortId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 8)}..`;
}

export function PolicyList({ policies, loading }: PolicyListProps): React.ReactNode {
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
        <text>No policies configured</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  ID          NAME                 TYPE        LIMIT        PERIOD     ENABLED"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ----------  -------------------  ----------  -----------  ---------  -------"}</text>
      </box>

      {/* Rows */}
      {policies.map((p) => {
        const enabled = p.enabled ? "yes" : "no";
        const limit = p.limit_amount ?? "-";
        const period = p.period ?? "-";
        const name = p.name.length > 19 ? `${p.name.slice(0, 16)}...` : p.name;

        return (
          <box key={p.policy_id} height={1} width="100%">
            <text>
              {`  ${shortId(p.policy_id).padEnd(10)}  ${name.padEnd(19)}  ${p.type.padEnd(10)}  ${limit.padEnd(11)}  ${period.padEnd(9)}  ${enabled}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
