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
import { useCopy } from "../../shared/hooks/use-copy.js";
import { jumpToStart, jumpToEnd } from "../../shared/hooks/use-list-navigation.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { BrickGate } from "../../shared/components/brick-gate.js";
import { TransactionList } from "./transaction-list.js";
import { EntryDetail } from "./entry-detail.js";
import { ConflictsView } from "./conflicts-tab.js";
import { useUiStore } from "../../stores/ui-store.js";
import { focusColor } from "../../shared/theme.js";

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

  const conflicts = useVersionsStore((s) => s.conflicts);
  const conflictsLoading = useVersionsStore((s) => s.conflictsLoading);
  const showConflicts = useVersionsStore((s) => s.showConflicts);

  const transactionDetail = useVersionsStore((s) => s.transactionDetail);
  const transactionDetailLoading = useVersionsStore((s) => s.transactionDetailLoading);
  const diffContent = useVersionsStore((s) => s.diffContent);
  const diffLoading = useVersionsStore((s) => s.diffLoading);
  const fetchTransactionDetail = useVersionsStore((s) => s.fetchTransactionDetail);

  const fetchTransactions = useVersionsStore((s) => s.fetchTransactions);
  const setSelectedIndex = useVersionsStore((s) => s.setSelectedIndex);
  const setStatusFilter = useVersionsStore((s) => s.setStatusFilter);
  const fetchEntries = useVersionsStore((s) => s.fetchEntries);
  const fetchDiff = useVersionsStore((s) => s.fetchDiff);
  const beginTransaction = useVersionsStore((s) => s.beginTransaction);
  const commitTransaction = useVersionsStore((s) => s.commitTransaction);
  const rollbackTransaction = useVersionsStore((s) => s.rollbackTransaction);
  const fetchConflicts = useVersionsStore((s) => s.fetchConflicts);
  const toggleConflicts = useVersionsStore((s) => s.toggleConflicts);

  // Clipboard copy
  const { copy, copied } = useCopy();

  // Focus pane (ui-store)
  const uiFocusPane = useUiStore((s) => s.getFocusPane("versions"));
  const toggleFocus = useUiStore((s) => s.toggleFocusPane);
  const overlayActive = useUiStore((s) => s.overlayActive);

  // Fetch transactions on mount and when filter changes
  useEffect(() => {
    if (client) {
      fetchTransactions(client);
    }
  }, [client, statusFilter, fetchTransactions]);

  // Fetch entries and transaction detail when selection changes
  useEffect(() => {
    if (client && selectedTransaction) {
      fetchEntries(selectedTransaction.transaction_id, client);
      fetchTransactionDetail(selectedTransaction.transaction_id, client);
    }
  }, [client, selectedTransaction, fetchEntries, fetchTransactionDetail]);

  // Keyboard navigation
  useKeyboard(overlayActive ? {} : {
    "j": () => {
      if (transactions.length === 0) return;
      setSelectedIndex(Math.max(0, Math.min(selectedIndex + 1, transactions.length - 1)));
    },
    "down": () => {
      if (transactions.length === 0) return;
      setSelectedIndex(Math.max(0, Math.min(selectedIndex + 1, transactions.length - 1)));
    },
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
    "v": () => {
      // View diff for the first entry of the selected transaction
      if (!client || !selectedTransaction || entries.length === 0) return;
      const entry = entries[0];
      if (entry && entry.original_hash && entry.new_hash) {
        fetchDiff(entry.path, entry.original_hash, entry.new_hash, client);
      }
    },
    "c": () => {
      // Toggle conflicts view; fetch on first open
      toggleConflicts();
      if (!showConflicts && client) {
        fetchConflicts(client);
      }
    },
    "tab": () => toggleFocus("versions"),
    "g": () => setSelectedIndex(jumpToStart()),
    "shift+g": () => setSelectedIndex(jumpToEnd(transactions.length)),
    "y": () => {
      if (selectedTransaction) {
        copy(selectedTransaction.transaction_id);
      }
    },
  });

  const filterLabel = statusFilter ? ` [${statusFilter}]` : " [all]";

  return (
    <BrickGate brick="versioning">
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
          <box width="40%" height="100%" borderStyle="single" borderColor={uiFocusPane === "left" ? focusColor.activeBorder : focusColor.inactiveBorder}>
            <TransactionList
              transactions={transactions}
              selectedIndex={selectedIndex}
            />
          </box>

          {/* Right pane: entry detail (60%) */}
          <box width="60%" height="100%" borderStyle="single" borderColor={uiFocusPane === "right" ? focusColor.activeBorder : focusColor.inactiveBorder}>
            <EntryDetail
              transaction={selectedTransaction}
              entries={entries}
              isLoading={entriesLoading}
            />
          </box>
        </box>

        {/* Transaction detail (below entry detail) */}
        {transactionDetail && !transactionDetailLoading && (
          <box height={3} width="100%">
            <text>
              {`Detail: zone=${transactionDetail.zone_id} agent=${transactionDetail.agent_id ?? "n/a"} entries=${transactionDetail.entry_count} created=${transactionDetail.created_at} expires=${transactionDetail.expires_at}`}
            </text>
          </box>
        )}

        {/* Diff viewer */}
        {diffContent && !diffLoading && (
          <box height={5} width="100%" borderStyle="single" flexDirection="column">
            <box height={1} width="100%"><text>--- Old ---</text></box>
            <box width="100%"><text>{diffContent.old.slice(0, 200)}</text></box>
            <box height={1} width="100%"><text>--- New ---</text></box>
            <box width="100%"><text>{diffContent.new.slice(0, 200)}</text></box>
          </box>
        )}

        {/* Conflicts pane (toggleable) */}
        <ConflictsView
          conflicts={conflicts}
          loading={conflictsLoading}
          visible={showConflicts}
        />

        {/* Help bar */}
        <box height={1} width="100%">
          {copied
            ? <text foregroundColor="green">Copied!</text>
            : <text>
            {"j/k:navigate  n:new txn  Enter:commit  Backspace:rollback  f:filter  d:diff  c:conflicts  y:copy  q:quit"}
          </text>}
        </box>
      </box>
    </BrickGate>
  );
}
