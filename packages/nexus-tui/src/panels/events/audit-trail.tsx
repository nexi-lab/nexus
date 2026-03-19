/**
 * Audit trail explorer view.
 *
 * Shows full audit transactions from GET /api/v2/audit/transactions
 * with cursor-based pagination.
 */

import React from "react";
import type { AuditTransaction } from "../../stores/infra-store.js";
import { Spinner } from "../../shared/components/spinner.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { formatTimestamp } from "../../shared/utils/format-time.js";

export interface AuditTrailProps {
  readonly transactions: readonly AuditTransaction[];
  readonly loading: boolean;
  readonly hasMore: boolean;
  readonly selectedIndex: number;
}

export function AuditTrail({
  transactions,
  loading,
  hasMore,
  selectedIndex,
}: AuditTrailProps): React.ReactNode {
  if (loading && transactions.length === 0) {
    return <Spinner label="Loading audit transactions..." />;
  }

  if (transactions.length === 0) {
    return (
      <EmptyState
        message="No audit transactions found."
        hint="Transactions will appear as actions are audited."
      />
    );
  }

  const displayTransactions = transactions.slice(0, 200);
  const isTruncated = transactions.length > 200;

  return (
    <box flexDirection="column" height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>
          {isTruncated
            ? `  Showing first 200 of ${transactions.length} — Action           Actor              Resource                       Status    Time`
            : "  Action           Actor              Resource                       Status    Time"}
        </text>
      </box>

      {/* Rows */}
      <scrollbox flexGrow={1} width="100%">
        {displayTransactions.map((tx, i) => {
          const prefix = i === selectedIndex ? "> " : "  ";
          const action = tx.action.padEnd(16).slice(0, 16);
          const actor = tx.actor_id.padEnd(18).slice(0, 18);
          const resource = tx.resource.padEnd(30).slice(0, 30);
          const status = tx.status.padEnd(9).slice(0, 9);
          const time = formatTimestamp(tx.timestamp);
          return (
            <box key={tx.transaction_id} height={1} width="100%">
              <text>{`${prefix}${action} ${actor} ${resource} ${status} ${time}`}</text>
            </box>
          );
        })}
        {hasMore && <text dimColor>{"  ... more transactions (press m to load more)"}</text>}
        {loading && transactions.length > 0 && <text dimColor>{"  Loading..."}</text>}
      </scrollbox>
    </box>
  );
}
