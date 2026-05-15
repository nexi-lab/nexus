/**
 * Scrollable transaction list with status badges and selection highlight.
 */

import { For, Show } from "solid-js";
import type { Transaction } from "../../stores/versions-store.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { textStyle } from "../../shared/text-style.js";
import { transactionStatusColor } from "../../shared/theme.js";
import { ScrollIndicator } from "../../shared/components/scroll-indicator.js";

// =============================================================================
// Status badges
// =============================================================================

const STATUS_BADGE: Readonly<Record<Transaction["status"], string>> = {
  active: "\u25CF",       // filled circle
  committed: "\u2713",    // check mark
  rolled_back: "\u2717",  // ballot x
  expired: "\u25CB",      // empty circle
};

function statusBadge(status: Transaction["status"]): string {
  return STATUS_BADGE[status];
}

function truncateId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id;
}

function formatTime(iso: string): string {
  try {
    const date = new Date(iso);
    return date.toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

// =============================================================================
// Component
// =============================================================================

interface TransactionListProps {
  readonly transactions: readonly Transaction[];
  readonly selectedIndex: number;
}

export function TransactionList(props: TransactionListProps) {
  return (
    <Show
      when={props.transactions.length > 0}
      fallback={
        <EmptyState
          message="No transactions yet."
          hint="Press n to begin one, or write a file to create an auto-transaction."
        />
      }
    >
      <ScrollIndicator selectedIndex={props.selectedIndex} totalItems={props.transactions.length} visibleItems={20}>
        <scrollbox flexGrow={1} width="100%">
          <For each={props.transactions}>{(txn, index) => {
            const selected = () => index() === props.selectedIndex;
            const badge = statusBadge(txn.status);
            const desc = txn.description ?? "";
            const id = truncateId(txn.transaction_id);
            const time = formatTime(txn.created_at);
            const entries = `${txn.entry_count} entries`;

            return (
              <box height={1} width="100%">
                <text>{selected() ? "\u25B8 " : "  "}</text>
                <text style={textStyle({ fg: transactionStatusColor[txn.status] })}>{badge}</text>
                <text>
                  {` ${id}  ${desc ? desc + "  " : ""}${entries}  ${time}`}
                </text>
              </box>
            );
          }}</For>
        </scrollbox>
      </ScrollIndicator>
    </Show>
  );
}
