/**
 * Drift view: shows drift report with has_drift flag, count, and drifted paths.
 */

import React from "react";
import type { DriftReport } from "../../stores/zones-store.js";

interface DriftViewProps {
  readonly drift: DriftReport | null;
  readonly loading: boolean;
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function DriftView({ drift, loading }: DriftViewProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading drift report...</text>
      </box>
    );
  }

  if (!drift) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Select a brick to view drift</text>
      </box>
    );
  }

  const driftLabel = drift.has_drift ? "YES - drift detected" : "No drift";

  return (
    <scrollbox height="100%" width="100%">
      <box height={1} width="100%">
        <text>{`Drift:        ${driftLabel}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Drift count:  ${drift.drift_count}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Last checked: ${formatTimestamp(drift.last_checked)}`}</text>
      </box>

      {drift.drifted_paths.length > 0 && (
        <>
          <box height={1} width="100%" marginTop={1}>
            <text>--- Drifted Paths ---</text>
          </box>
          {drift.drifted_paths.map((path, i) => (
            <box key={`drift-${i}`} height={1} width="100%">
              <text>{`  ${i + 1}. ${path}`}</text>
            </box>
          ))}
        </>
      )}
    </scrollbox>
  );
}
