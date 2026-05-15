/**
 * Snapshot entry detail view for the selected transaction.
 *
 * Shows each entry's operation, path, and hash changes.
 * Supports keyboard selection (selectedEntryIndex) and mouse clicks.
 */

import { For, Show } from "solid-js";
import { textStyle } from "../../shared/text-style.js";
import { focusColor } from "../../shared/theme.js";
import type { SnapshotEntry, Transaction } from "../../stores/versions-store.js";

// =============================================================================
// Operation badges
// =============================================================================

const OPERATION_BADGE: Readonly<Record<SnapshotEntry["operation"], string>> = {
  write: "W",
  delete: "D",
  rename: "R",
};

function truncateHash(hash: string | null): string {
  if (!hash) return "-";
  return hash.length > 8 ? hash.slice(0, 8) : hash;
}

// =============================================================================
// Component
// =============================================================================

interface EntryDetailProps {
  readonly transaction: Transaction | null;
  readonly entries: readonly SnapshotEntry[];
  readonly isLoading: boolean;
  readonly selectedEntryIndex: number;
  readonly onSelectEntry: (index: number) => void;
  readonly focused: boolean;
}

export function EntryDetail(props: EntryDetailProps) {
  return (
    <Show
      when={props.transaction}
      fallback={
        <box height="100%" width="100%" justifyContent="center" alignItems="center">
          <text>Select a transaction to view entries</text>
        </box>
      }
    >
      <box height="100%" width="100%" flexDirection="column">
        {/* Header */}
        <box height={2} width="100%" flexDirection="column">
          <text>{`Transaction: ${props.transaction!.transaction_id}`}</text>
          <text>{`Status: ${props.transaction!.status}  Entries: ${props.transaction!.entry_count}`}</text>
        </box>

        {/* Entry list */}
        <Show
          when={!props.isLoading}
          fallback={
          <box flexGrow={1} justifyContent="center" alignItems="center">
            <text>Loading entries...</text>
          </box>
          }
        >
          <Show
            when={props.entries.length > 0}
            fallback={
              <box flexGrow={1} justifyContent="center" alignItems="center">
                <text>No entries in this transaction</text>
              </box>
            }
          >
          <scrollbox flexGrow={1} width="100%">
            {/* Column headers */}
            <box height={1} width="100%">
              <text>{"  OP  PATH                             OLD_HASH    NEW_HASH"}</text>
            </box>
            <box height={1} width="100%">
              <text>{"  --  -------------------------------  ----------  ----------"}</text>
            </box>
            <For each={props.entries}>{(entry, index) => {
              const badge = OPERATION_BADGE[entry.operation];
              const original = truncateHash(entry.original_hash);
              const next = truncateHash(entry.new_hash);
              const isSelected = () => index() === props.selectedEntryIndex;

              return (
                <box
                  height={1}
                  width="100%"
                  onMouseDown={() => props.onSelectEntry(index())}
                >
                  <text style={isSelected() ? textStyle({ fg: focusColor.activeBorder, bold: true }) : undefined}>
                    {`${isSelected() && props.focused ? "▶" : " "} [${badge}] ${entry.path}  ${original}  ${next}`}
                  </text>
                </box>
              );
            }}</For>
          </scrollbox>
          </Show>
        </Show>
      </box>
    </Show>
  );
}
