/**
 * Conflicts view for the Versions & Snapshots panel.
 * Displayed as a toggleable bottom pane (press 'c' to show/hide).
 */

import React from "react";
import type { ConflictItem } from "../../stores/versions-store.js";

interface ConflictsViewProps {
  readonly conflicts: readonly ConflictItem[];
  readonly loading: boolean;
  readonly visible: boolean;
}

export function ConflictsView({
  conflicts,
  loading,
  visible,
}: ConflictsViewProps): React.ReactNode {
  if (!visible) return null;

  if (loading) {
    return (
      <box height={6} width="100%" borderStyle="single" flexDirection="column">
        <text>{"--- Conflicts ---"}</text>
        <text>Loading conflicts...</text>
      </box>
    );
  }

  if (conflicts.length === 0) {
    return (
      <box height={4} width="100%" borderStyle="single" flexDirection="column">
        <text>{"--- Conflicts ---"}</text>
        <text>No conflicts detected.</text>
      </box>
    );
  }

  return (
    <box
      height={Math.min(conflicts.length + 3, 12)}
      width="100%"
      borderStyle="single"
      flexDirection="column"
    >
      <text>{"--- Conflicts ---"}</text>
      <scrollbox height="100%" width="100%">
        {conflicts.map((conflict, i) => (
          <box key={`${conflict.path}-${i}`} height={1} width="100%">
            <text>
              {`  ${conflict.path}  ${conflict.reason}  expected:${conflict.expected_hash ?? "n/a"}  current:${conflict.current_hash ?? "n/a"}  txn:${conflict.transaction_id ?? "n/a"}`}
            </text>
          </box>
        ))}
      </scrollbox>
    </box>
  );
}
