/**
 * Transaction list: displays audit transaction records with status, flow,
 * pagination controls, and integrity verification result.
 */

import React from "react";
import type { TransactionRecord, IntegrityResult } from "../../stores/payments-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";

interface TransactionListProps {
  readonly transactions: readonly TransactionRecord[];
  readonly selectedIndex: number;
  readonly loading: boolean;
  readonly hasMore: boolean;
  readonly hasPrev: boolean;
  readonly currentPage: number;
  readonly integrityResult: IntegrityResult | null;
}

function shortId(id: string): string {
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

function formatAmount(amount: string, currency: string): string {
  return `${amount} ${currency}`;
}

export function TransactionList({
  transactions,
  selectedIndex,
  loading,
  hasMore,
  hasPrev,
  currentPage,
  integrityResult,
}: TransactionListProps): React.ReactNode {
  if (loading) {
    return <LoadingIndicator message="Loading transactions..." />;
  }

  if (transactions.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No transactions found</text>
      </box>
    );
  }

  const selectedTx = transactions[selectedIndex];

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  ID          DATE                 AMOUNT          STATUS     FLOW"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ----------  -------------------  --------------  ---------  --------------------"}</text>
      </box>

      {/* Rows */}
      <scrollbox flexGrow={1} width="100%">
        {transactions.map((tx, i) => {
          const isSelected = i === selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const flow = `${shortId(tx.buyer_agent_id)}->${shortId(tx.seller_agent_id)}`;

          return (
            <box key={tx.id} height={1} width="100%">
              <text>
                {`${prefix}${shortId(tx.id).padEnd(10)}  ${formatTimestamp(tx.created_at).padEnd(19)}  ${formatAmount(tx.amount, tx.currency).padEnd(14)}  ${tx.status.padEnd(9)}  ${flow}`}
              </text>
            </box>
          );
        })}
      </scrollbox>

      {/* Integrity verification result */}
      {integrityResult && selectedTx && integrityResult.record_id === selectedTx.id && (
        <box height={1} width="100%">
          <text>
            {integrityResult.is_valid
              ? `Integrity OK: ${shortId(integrityResult.record_hash)} (valid)`
              : `INTEGRITY FAIL: ${shortId(integrityResult.record_hash)} (TAMPERED)`}
          </text>
        </box>
      )}

      {/* Pagination status */}
      <box height={1} width="100%">
        <text>
          {`  Page ${currentPage}${hasMore ? "+" : ""}  ${hasPrev ? "[p:prev]" : ""}  ${hasMore ? "[n:next]" : "(end)"}  ${transactions.length} shown`}
        </text>
      </box>
    </box>
  );
}
