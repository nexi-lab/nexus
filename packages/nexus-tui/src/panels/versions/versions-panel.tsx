/**
 * Versions & Snapshots panel.
 *
 * Left pane: transaction list with status badges.
 * Right pane: entry detail for the selected transaction.
 * Bottom: keyboard shortcut hints.
 */

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useTerminalDimensions } from "@opentui/react";
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
import { DiffViewer } from "../../shared/components/diff-viewer.js";
import { useUiStore } from "../../stores/ui-store.js";
import { focusColor } from "../../shared/theme.js";
import { textStyle } from "../../shared/text-style.js";
import { formatActionHints, getVersionsFooterBindings } from "../../shared/action-registry.js";

export default function VersionsPanel(): React.ReactNode {
  const client = useApi();
  const { width: columns } = useTerminalDimensions();
  const isNarrow = columns < 120;

  const transactions = useVersionsStore((s) => s.transactions);
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
  const clearDiff = useVersionsStore((s) => s.clearDiff);

  // Clipboard copy
  const { copy, copied } = useCopy();

  // Focus pane (ui-store)
  const uiFocusPane = useUiStore((s) => s.getFocusPane("versions"));
  const toggleFocus = useUiStore((s) => s.toggleFocusPane);
  const overlayActive = useUiStore((s) => s.overlayActive);

  // Entry selection within the right pane
  const [selectedEntryIndex, setSelectedEntryIndex] = useState(0);

  // Transaction search/filter
  const [txnFilterMode, setTxnFilterMode] = useState(false);
  const [txnFilter, setTxnFilter] = useState("");

  const filteredTransactions = useMemo(() => {
    if (!txnFilter) return transactions;
    const lower = txnFilter.toLowerCase();
    return transactions.filter(
      (t) =>
        t.transaction_id.toLowerCase().includes(lower) ||
        (t.description ?? "").toLowerCase().includes(lower),
    );
  }, [transactions, txnFilter]);

  // Derive selectedTransaction from filtered list so the index always maps correctly
  const selectedTransaction = filteredTransactions[selectedIndex] ?? null;

  const handleFilterKey = useCallback(
    (keyName: string) => {
      if (!txnFilterMode) return;
      if (keyName.length === 1) {
        setTxnFilter((b) => b + keyName);
      } else if (keyName === "space") {
        setTxnFilter((b) => b + " ");
      }
    },
    [txnFilterMode],
  );

  // Fetch transactions on mount and when filter changes
  useEffect(() => {
    if (client) {
      fetchTransactions(client);
    }
  }, [client, statusFilter, fetchTransactions]);

  // Fetch entries and transaction detail when selection changes; reset entry cursor and diff
  useEffect(() => {
    setSelectedEntryIndex(0);
    clearDiff();
    if (client && selectedTransaction) {
      fetchEntries(selectedTransaction.transaction_id, client);
      fetchTransactionDetail(selectedTransaction.transaction_id, client);
    }
  }, [client, selectedTransaction, fetchEntries, fetchTransactionDetail, clearDiff]);

  // Keyboard navigation
  useKeyboard(
    overlayActive
      ? {}
      : txnFilterMode
        ? {
            return: () => {
              setTxnFilterMode(false);
              setSelectedIndex(0);
            },
            escape: () => {
              setTxnFilterMode(false);
              setTxnFilter("");
              setSelectedIndex(0);
            },
            backspace: () => {
              setTxnFilter((b) => b.slice(0, -1));
            },
          }
        : uiFocusPane === "right"
          ? {
              // Right pane focused: j/k navigates entries and auto-fetches diff
              "j": () => {
                const next = Math.min(selectedEntryIndex + 1, entries.length - 1);
                setSelectedEntryIndex(next);
                const entry = entries[next];
                if (client && entry) fetchDiff(entry.path, entry.original_hash, entry.new_hash, selectedTransaction?.transaction_id ?? "", client);
              },
              "down": () => {
                const next = Math.min(selectedEntryIndex + 1, entries.length - 1);
                setSelectedEntryIndex(next);
                const entry = entries[next];
                if (client && entry) fetchDiff(entry.path, entry.original_hash, entry.new_hash, selectedTransaction?.transaction_id ?? "", client);
              },
              "k": () => {
                const prev = Math.max(selectedEntryIndex - 1, 0);
                setSelectedEntryIndex(prev);
                const entry = entries[prev];
                if (client && entry) fetchDiff(entry.path, entry.original_hash, entry.new_hash, selectedTransaction?.transaction_id ?? "", client);
              },
              "up": () => {
                const prev = Math.max(selectedEntryIndex - 1, 0);
                setSelectedEntryIndex(prev);
                const entry = entries[prev];
                if (client && entry) fetchDiff(entry.path, entry.original_hash, entry.new_hash, selectedTransaction?.transaction_id ?? "", client);
              },
              "tab": () => toggleFocus("versions"),
            }
          : {
              // Left pane focused: j/k navigates transactions
              "j": () => {
                if (filteredTransactions.length === 0) return;
                setSelectedIndex(Math.max(0, Math.min(selectedIndex + 1, filteredTransactions.length - 1)));
              },
              "down": () => {
                if (filteredTransactions.length === 0) return;
                setSelectedIndex(Math.max(0, Math.min(selectedIndex + 1, filteredTransactions.length - 1)));
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
              "/": () => {
                setTxnFilterMode(true);
                setTxnFilter("");
              },
              "v": () => {
                // Diff the selected entry (defaults to index 0)
                if (!client || !selectedTransaction || entries.length === 0) return;
                const entry = entries[selectedEntryIndex];
                if (entry) {
                  fetchDiff(entry.path, entry.original_hash, entry.new_hash, selectedTransaction.transaction_id, client);
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
              "shift+g": () => setSelectedIndex(jumpToEnd(filteredTransactions.length)),
              "y": () => {
                if (selectedTransaction) {
                  copy(selectedTransaction.transaction_id);
                }
              },
            },
    txnFilterMode ? handleFilterKey : undefined,
  );

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
                : `Versions & Snapshots${filterLabel} -- ${filteredTransactions.length} transactions${txnFilter ? ` (filtered)` : ""}`}
          </text>
        </box>

        {/* Filter bar */}
        {txnFilterMode && (
          <box height={1} width="100%">
            <text>{`Search: ${txnFilter}\u2588`}</text>
          </box>
        )}
        {!txnFilterMode && txnFilter && (
          <box height={1} width="100%">
            <text>{`Filter: "${txnFilter}" (/ to change, Esc in filter to clear)`}</text>
          </box>
        )}

        {/* Main content: transaction list + entry detail */}
        <box flexGrow={1} flexDirection={isNarrow ? "column" : "row"}>
          {/* Left pane: transaction list (40% wide / 40% tall on narrow) */}
          <box
            width={isNarrow ? "100%" : "40%"}
            height={isNarrow ? "40%" : "100%"}
            borderStyle="single"
            borderColor={uiFocusPane === "left" ? focusColor.activeBorder : focusColor.inactiveBorder}
          >
            <TransactionList
              transactions={filteredTransactions}
              selectedIndex={selectedIndex}
            />
          </box>

          {/* Right pane: entry detail (60% wide / 60% tall on narrow) */}
          <box
            width={isNarrow ? "100%" : "60%"}
            height={isNarrow ? "60%" : "100%"}
            borderStyle="single"
            borderColor={uiFocusPane === "right" ? focusColor.activeBorder : focusColor.inactiveBorder}
          >
            <EntryDetail
              transaction={selectedTransaction}
              entries={entries}
              isLoading={entriesLoading}
              selectedEntryIndex={selectedEntryIndex}
              focused={uiFocusPane === "right"}
              onSelectEntry={(index) => {
                setSelectedEntryIndex(index);
                const entry = entries[index];
                if (client && entry && selectedTransaction) {
                  fetchDiff(entry.path, entry.original_hash, entry.new_hash, selectedTransaction.transaction_id, client);
                }
              }}
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
          <box height={12} width="100%">
            <DiffViewer oldText={diffContent.old} newText={diffContent.new} oldLabel="old" newLabel="new" />
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
            ? <text style={textStyle({ fg: "green" })}>Copied!</text>
            : <text>
            {formatActionHints(getVersionsFooterBindings({ txnFilterMode }))}
          </text>}
        </box>
      </box>
    </BrickGate>
  );
}
