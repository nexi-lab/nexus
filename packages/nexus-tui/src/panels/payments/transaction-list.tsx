/**
 * Transaction list: displays audit transaction records with status, flow,
 * pagination controls, and integrity verification result.
 */

import { For, Show } from "solid-js";
import type { TransactionRecord, IntegrityResult } from "../../stores/payments-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { EmptyState } from "../../shared/components/empty-state.js";

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

export function TransactionList(props: TransactionListProps) {
  if (props.loading) {
    return <LoadingIndicator message="Loading transactions..." />;
  }

  if (props.transactions.length === 0) {
    return <EmptyState message="No transactions yet." hint="Press t to create a transfer." />;
  }

  const selectedTx = () => props.transactions[props.selectedIndex];

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
        <For each={props.transactions}>{(tx, i) => {
          const flow = `${shortId(tx.buyer_agent_id)}->${shortId(tx.seller_agent_id)}`;

          return (
            <box height={1} width="100%">
              <text>
                {`${i() === props.selectedIndex ? "> " : "  "}${shortId(tx.id).padEnd(10)}  ${formatTimestamp(tx.created_at).padEnd(19)}  ${formatAmount(tx.amount, tx.currency).padEnd(14)}  ${tx.status.padEnd(9)}  ${flow}`}
              </text>
            </box>
          );
        }}</For>
      </scrollbox>

      {/* Integrity verification result */}
      <Show when={props.integrityResult && selectedTx() && props.integrityResult.record_id === selectedTx()!.id}>
        <box height={1} width="100%">
          <text>
            {props.integrityResult!.is_valid
              ? `Integrity OK: ${shortId(props.integrityResult!.record_hash)} (valid)`
              : `INTEGRITY FAIL: ${shortId(props.integrityResult!.record_hash)} (TAMPERED)`}
          </text>
        </box>
      </Show>

      {/* Pagination status */}
      <box height={1} width="100%">
        <text>
          {`  Page ${props.currentPage}${props.hasMore ? "+" : ""}  ${props.hasPrev ? "[p:prev]" : ""}  ${props.hasMore ? "[n:next]" : "(end)"}  ${props.transactions.length} shown`}
        </text>
      </box>
    </box>
  );
}
