/**
 * Versions & Snapshots panel.
 *
 * Left pane: transaction list with status badges.
 * Right pane: entry detail for the selected transaction.
 * Bottom: keyboard shortcut hints.
 */

import React, { useEffect } from "react";
import {
  useVersionsStore,
  nextStatusFilter,
} from "../../stores/versions-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { TransactionList } from "./transaction-list.js";
import { EntryDetail } from "./entry-detail.js";
import { TransactionActions } from "./transaction-actions.js";

export default function VersionsPanel(): React.ReactNode {
  const client = useApi();

  const transactions = useVersionsStore((s) => s.transactions);
  const selectedTransaction = useVersionsStore((s) => s.selectedTransaction);
  const selectedIndex = useVersionsStore((s) => s.selectedIndex);
  const statusFilter = useVersionsStore((s) => s.statusFilter);
  const isLoading = useVersionsStore((s) => s.isLoading);
  const error = useVersionsStore((s) => s.error);
  const entries = useVersionsStore((s) => s.entries);
  const entriesLoading = useVersionsStore((s) => s.entriesLoading);

  const fetchTransactions = useVersionsStore((s) => s.fetchTransactions);
  const setSelectedIndex = useVersionsStore((s) => s.setSelectedIndex);
  const setStatusFilter = useVersionsStore((s) => s.setStatusFilter);
  const fetchEntries = useVersionsStore((s) => s.fetchEntries);
  const beginTransaction = useVersionsStore((s) => s.beginTransaction);
  const commitTransaction = useVersionsStore((s) => s.commitTransaction);
  const rollbackTransaction = useVersionsStore((s) => s.rollbackTransaction);

  // Fetch transactions on mount and when filter changes
  useEffect(() => {
    if (client) {
      fetchTransactions(client);
    }
  }, [client, statusFilter, fetchTransactions]);

  // Fetch entries when selection changes
  useEffect(() => {
    if (client && selectedTransaction) {
      fetchEntries(selectedTransaction.transaction_id, client);
    }
  }, [client, selectedTransaction, fetchEntries]);

  // Keyboard navigation
  useKeyboard({
    "j": () => setSelectedIndex(Math.min(selectedIndex + 1, transactions.length - 1)),
    "down": () =>
      setSelectedIndex(Math.min(selectedIndex + 1, transactions.length - 1)),
    "k": () => setSelectedIndex(Math.max(selectedIndex - 1, 0)),
    "up": () => setSelectedIndex(Math.max(selectedIndex - 1, 0)),
    "return": () => {
      if (selectedTransaction?.status === "active" && client) {
        commitTransaction(selectedTransaction.transaction_id, client);
      }
    },
    "backspace": () => {
      if (selectedTransaction?.status === "active" && client) {
        rollbackTransaction(selectedTransaction.transaction_id, client);
      }
    },
    "n": () => {
      if (client) {
        beginTransaction(client);
      }
    },
    "f": () => {
      const next = nextStatusFilter(statusFilter);
      setStatusFilter(next);
    },
  });

  const filterLabel = statusFilter ? ` [${statusFilter}]` : " [all]";

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Title bar */}
      <box height={1} width="100%">
        <text>
          {isLoading
            ? `Versions & Snapshots${filterLabel} -- loading...`
            : error
              ? `Versions & Snapshots${filterLabel} -- error: ${error}`
              : `Versions & Snapshots${filterLabel} -- ${transactions.length} transactions`}
        </text>
      </box>

      {/* Main content: transaction list + entry detail */}
      <box flexGrow={1} flexDirection="row">
        {/* Left pane: transaction list (40%) */}
        <box width="40%" height="100%" borderStyle="single">
          <TransactionList
            transactions={transactions}
            selectedIndex={selectedIndex}
          />
        </box>

        {/* Right pane: entry detail (60%) */}
        <box width="60%" height="100%" borderStyle="single">
          <EntryDetail
            transaction={selectedTransaction}
            entries={entries}
            isLoading={entriesLoading}
          />
        </box>
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <TransactionActions transaction={selectedTransaction} />
      </box>
    </box>
  );
}
