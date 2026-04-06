/**
 * Versions & Snapshots panel.
 *
 * Left pane: transaction list with status badges.
 * Right pane: entry detail for the selected transaction.
 * Bottom: keyboard shortcut hints.
 */

import { createEffect, createMemo, createSignal, onCleanup } from "solid-js";
import type { JSX } from "solid-js";
import { useTerminalDimensions } from "@opentui/solid";
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

export default function VersionsPanel(): JSX.Element {
  const client = useApi();
  const termDims = useTerminalDimensions();
  const columns = () => termDims().width;
  const isNarrow = () => columns() < 120;

  // Rendering trigger — incremented on every store change so memos/JSX re-evaluate.
  const [_rev, _setRev] = createSignal(0);
  const unsubVersions = useVersionsStore.subscribe(() => _setRev((r) => r + 1));
  onCleanup(unsubVersions);

  // Read store values through getState() with _rev() dependency for reactivity
  const vs = () => { void _rev(); return useVersionsStore.getState(); };
  const transactions = () => vs().transactions;
  const selectedIndex = () => vs().selectedIndex;
  const isLoading = () => vs().isLoading;
  const error = () => vs().error;
  const entries = () => vs().entries;
  const entriesLoading = () => vs().entriesLoading;
  const conflicts = () => vs().conflicts;
  const conflictsLoading = () => vs().conflictsLoading;
  const showConflicts = () => vs().showConflicts;
  const transactionDetail = () => vs().transactionDetail;
  const transactionDetailLoading = () => vs().transactionDetailLoading;
  const diffContent = () => vs().diffContent;
  const diffLoading = () => vs().diffLoading;
  const statusFilter = () => vs().statusFilter;
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

  // Focus pane (ui-store) — subscribe for reactivity
  const [_uiRev, _setUiRev] = createSignal(0);
  const unsubUi = useUiStore.subscribe(() => _setUiRev((r) => r + 1));
  onCleanup(unsubUi);
  const uiState = () => { void _uiRev(); return useUiStore.getState(); };
  const uiFocusPane = () => uiState().getFocusPane("versions");
  const toggleFocus = useUiStore.getState().toggleFocusPane;
  const overlayActive = () => uiState().overlayActive;

  // Entry selection within the right pane
  const [selectedEntryIndex, setSelectedEntryIndex] = createSignal(0);

  // Transaction search/filter
  const [txnFilterMode, setTxnFilterMode] = createSignal(false);
  const [txnFilter, setTxnFilter] = createSignal("");

  const filteredTransactions = createMemo(() => {
    if (!txnFilter()) return transactions();
    const lower = txnFilter().toLowerCase();
    return transactions().filter(
      (t) =>
        t.transaction_id.toLowerCase().includes(lower) ||
        (t.description ?? "").toLowerCase().includes(lower),
    );
  });

  // Derive selectedTransaction from filtered list so the index always maps correctly
  const selectedTransaction = () => filteredTransactions()[selectedIndex()] ?? null;
  // Helper for keyboard handlers: read fresh state
  const gs = () => useVersionsStore.getState();

  const handleFilterKey = (keyName: string) => {
      if (!txnFilterMode()) return;
      if (keyName.length === 1) {
        setTxnFilter((b) => b + keyName);
      } else if (keyName === "space") {
        setTxnFilter((b) => b + " ");
      }
    };

  // Fetch transactions on mount and when filter changes
  createEffect(() => {
    if (client) {
      fetchTransactions(client);
    }
  });

  // Fetch entries and transaction detail when selection changes; reset entry cursor and diff
  createEffect(() => {
    setSelectedEntryIndex(0);
    clearDiff();
    const st = selectedTransaction();
    if (client && st) {
      fetchEntries(st.transaction_id, client);
      fetchTransactionDetail(st.transaction_id, client);
    }
  });

  // Keyboard navigation — wrapped in function for fresh state reads per keypress
  useKeyboard(
    (): Record<string, () => void> => {
      const ov = useUiStore.getState().overlayActive;
      if (ov) return {};
      if (txnFilterMode()) return {
        return: () => { setTxnFilterMode(false); setSelectedIndex(0); },
        escape: () => { setTxnFilterMode(false); setTxnFilter(""); setSelectedIndex(0); },
        backspace: () => { setTxnFilter((b) => b.slice(0, -1)); },
      };
      const s = gs();
      const ft = filteredTransactions();
      const st = selectedTransaction();
      const fp = useUiStore.getState().getFocusPane("versions");
      if (fp === "right") {
        const navEntry = (delta: number) => () => {
          const idx = delta > 0 ? Math.min(selectedEntryIndex() + 1, s.entries.length - 1) : Math.max(selectedEntryIndex() - 1, 0);
          setSelectedEntryIndex(idx);
          const entry = s.entries[idx];
          if (client && entry) fetchDiff(entry.path, entry.original_hash, entry.new_hash, st?.transaction_id ?? "", client);
        };
        return { j: navEntry(1), down: navEntry(1), k: navEntry(-1), up: navEntry(-1), tab: () => toggleFocus("versions") };
      }
      return {
        j: () => { if (ft.length > 0) setSelectedIndex(Math.min(s.selectedIndex + 1, ft.length - 1)); },
        down: () => { if (ft.length > 0) setSelectedIndex(Math.min(s.selectedIndex + 1, ft.length - 1)); },
        k: () => setSelectedIndex(Math.max(s.selectedIndex - 1, 0)),
        up: () => setSelectedIndex(Math.max(s.selectedIndex - 1, 0)),
        return: () => { if (st?.status === "active" && client) commitTransaction(st.transaction_id, client); },
        backspace: () => { if (st?.status === "active" && client) rollbackTransaction(st.transaction_id, client); },
        n: () => { if (client) beginTransaction(client); },
        f: () => { setStatusFilter(nextStatusFilter(s.statusFilter)); },
        "/": () => { setTxnFilterMode(true); setTxnFilter(""); },
        v: () => {
          if (!client || !st || s.entries.length === 0) return;
          const entry = s.entries[selectedEntryIndex()];
          if (entry) fetchDiff(entry.path, entry.original_hash, entry.new_hash, st.transaction_id, client);
        },
        c: () => { toggleConflicts(); if (!s.showConflicts && client) fetchConflicts(client); },
        tab: () => toggleFocus("versions"),
        g: () => setSelectedIndex(jumpToStart()),
        "shift+g": () => setSelectedIndex(jumpToEnd(ft.length)),
        y: () => { if (st) copy(st.transaction_id); },
      };
    },
    () => txnFilterMode() ? handleFilterKey : undefined,
  );

  const filterLabel = statusFilter() ? ` [${statusFilter()}]` : " [all]";

  return (
    <BrickGate brick="versioning">
      <box height="100%" width="100%" flexDirection="column">
        {/* Title bar */}
        <box height={1} width="100%">
          <text>
            {isLoading()
              ? `Versions & Snapshots${filterLabel} -- loading...`
              : error()
                ? `Versions & Snapshots${filterLabel} -- error: ${error()}`
                : `Versions & Snapshots${filterLabel} -- ${filteredTransactions().length} transactions${txnFilter() ? ` (filtered)` : ""}`}
          </text>
        </box>

        {/* Filter bar */}
        {txnFilterMode() && (
          <box height={1} width="100%">
            <text>{`Search: ${txnFilter()}\u2588`}</text>
          </box>
        )}
        {!txnFilterMode() && txnFilter() && (
          <box height={1} width="100%">
            <text>{`Filter: "${txnFilter()}" (/ to change, Esc in filter to clear)`}</text>
          </box>
        )}

        {/* Main content: transaction list + entry detail */}
        <box flexGrow={1} flexDirection={isNarrow() ? "column" : "row"}>
          {/* Left pane: transaction list (40% wide / 40% tall on narrow) */}
          <box
            width={isNarrow() ? "100%" : "40%"}
            height={isNarrow() ? "40%" : "100%"}
            borderStyle="single"
            borderColor={uiFocusPane() === "left" ? focusColor.activeBorder : focusColor.inactiveBorder}
          >
            <TransactionList
              transactions={filteredTransactions()}
              selectedIndex={selectedIndex()}
            />
          </box>

          {/* Right pane: entry detail (60% wide / 60% tall on narrow) */}
          <box
            width={isNarrow() ? "100%" : "60%"}
            height={isNarrow() ? "60%" : "100%"}
            borderStyle="single"
            borderColor={uiFocusPane() === "right" ? focusColor.activeBorder : focusColor.inactiveBorder}
          >
            <EntryDetail
              transaction={selectedTransaction()}
              entries={entries()}
              isLoading={entriesLoading()}
              selectedEntryIndex={selectedEntryIndex()}
              focused={uiFocusPane() === "right"}
              onSelectEntry={(index) => {
                setSelectedEntryIndex(index);
                const s = gs();
                const entry = s.entries[index];
                const st = selectedTransaction();
                if (client && entry && st) {
                  fetchDiff(entry.path, entry.original_hash, entry.new_hash, st.transaction_id, client);
                }
              }}
            />
          </box>
        </box>

        {/* Transaction detail (below entry detail) */}
        {transactionDetail() && !transactionDetailLoading() && (
          <box height={3} width="100%">
            <text>
              {`Detail: zone=${transactionDetail()!.zone_id} agent=${transactionDetail()!.agent_id ?? "n/a"} entries=${transactionDetail()!.entry_count} created=${transactionDetail()!.created_at} expires=${transactionDetail()!.expires_at}`}
            </text>
          </box>
        )}

        {/* Diff viewer */}
        {diffContent() && !diffLoading() && (
          <box height={12} width="100%">
            <DiffViewer oldText={diffContent()!.old} newText={diffContent()!.new} oldLabel="old" newLabel="new" />
          </box>
        )}

        {/* Conflicts pane (toggleable) */}
        <ConflictsView
          conflicts={conflicts()}
          loading={conflictsLoading()}
          visible={showConflicts()}
        />

        {/* Help bar */}
        <box height={1} width="100%">
          {copied
            ? <text style={textStyle({ fg: "green" })}>Copied!</text>
            : <text>
            {formatActionHints(getVersionsFooterBindings({ txnFilterMode: txnFilterMode() }))}
          </text>}
        </box>
      </box>
    </BrickGate>
  );
}
