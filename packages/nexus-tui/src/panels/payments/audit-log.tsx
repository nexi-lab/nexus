/**
 * Audit log: transaction history with timestamps, types, amounts, and statuses.
 */

import React from "react";
import type { AuditEntry } from "../../stores/payments-store.js";

interface AuditLogProps {
  readonly entries: readonly AuditEntry[];
  readonly total: number;
  readonly loading: boolean;
}

function shortId(id: string | null): string {
  if (!id) return "-";
  if (id.length <= 12) return id;
  return `${id.slice(0, 8)}..`;
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function AuditLog({ entries, total, loading }: AuditLogProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading audit log...</text>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Summary */}
      <box height={1} width="100%">
        <text>{`Audit log: ${total} total entr${total === 1 ? "y" : "ies"}`}</text>
      </box>

      {entries.length === 0 ? (
        <box flexGrow={1} justifyContent="center" alignItems="center">
          <text>No audit entries found</text>
        </box>
      ) : (
        <scrollbox flexGrow={1} width="100%">
          {/* Header */}
          <box height={1} width="100%">
            <text>{"  TYPE        AMOUNT       FROM        TO          STATUS      TIMESTAMP"}</text>
          </box>
          <box height={1} width="100%">
            <text>{"  ----------  -----------  ----------  ----------  ----------  -------------------"}</text>
          </box>

          {/* Rows */}
          {entries.map((e) => {
            const from = shortId(e.from_account);
            const to = shortId(e.to_account);

            return (
              <box key={e.entry_id} height={1} width="100%">
                <text>
                  {`  ${e.type.padEnd(10)}  ${e.amount.padEnd(11)}  ${from.padEnd(10)}  ${to.padEnd(10)}  ${e.status.padEnd(10)}  ${formatTimestamp(e.created_at)}`}
                </text>
              </box>
            );
          })}
        </scrollbox>
      )}
    </box>
  );
}
