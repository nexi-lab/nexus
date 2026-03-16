/**
 * Snapshot entry detail view for the selected transaction.
 *
 * Shows each entry's operation, path, and hash changes.
 */

import React from "react";
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
}

export function EntryDetail({
  transaction,
  entries,
  isLoading,
}: EntryDetailProps): React.ReactNode {
  if (!transaction) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Select a transaction to view entries</text>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={2} width="100%" flexDirection="column">
        <text>{`Transaction: ${transaction.transaction_id}`}</text>
        <text>{`Status: ${transaction.status}  Entries: ${transaction.entry_count}`}</text>
      </box>

      {/* Entry list */}
      {isLoading ? (
        <box flexGrow={1} justifyContent="center" alignItems="center">
          <text>Loading entries...</text>
        </box>
      ) : entries.length === 0 ? (
        <box flexGrow={1} justifyContent="center" alignItems="center">
          <text>No entries in this transaction</text>
        </box>
      ) : (
        <scrollbox flexGrow={1} width="100%">
          {entries.map((entry) => {
            const badge = OPERATION_BADGE[entry.operation];
            const original = truncateHash(entry.original_hash);
            const next = truncateHash(entry.new_hash);
            const hashStr =
              entry.original_hash || entry.new_hash
                ? `${original}\u2192${next}`
                : "";

            return (
              <box key={entry.entry_id} height={1} width="100%">
                <text>{`[${badge}] ${entry.path}  ${hashStr}`}</text>
              </box>
            );
          })}
        </scrollbox>
      )}
    </box>
  );
}
