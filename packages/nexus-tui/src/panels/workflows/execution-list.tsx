/**
 * Execution list view: status, started_at, duration, trigger, error.
 */

import React from "react";
import type { Execution } from "../../stores/workflows-store.js";

interface ExecutionListProps {
  readonly executions: readonly Execution[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

const STATUS_BADGES: Readonly<Record<Execution["status"], string>> = {
  running: "[RUN]",
  completed: "[OK ]",
  failed: "[ERR]",
  cancelled: "[CAN]",
};

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function formatDuration(ms: number | null): string {
  if (ms === null) return "---";
  if (ms < 1000) return `${ms}ms`;
  const seconds = (ms / 1000).toFixed(1);
  return `${seconds}s`;
}

export function ExecutionList({
  executions,
  selectedIndex,
  loading,
}: ExecutionListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading executions...</text>
      </box>
    );
  }

  if (executions.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No executions found</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  ST     STARTED              DURATION  TRIGGER         ERROR"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  -----  -------------------  --------  --------------  -----"}</text>
      </box>

      {/* Rows */}
      {executions.map((ex, i) => {
        const isSelected = i === selectedIndex;
        const badge = STATUS_BADGES[ex.status] ?? `[${ex.status.toUpperCase()}]`;
        const trigger = ex.trigger.length > 14
          ? `${ex.trigger.slice(0, 11)}...`
          : ex.trigger;
        const errorText = ex.error
          ? ex.error.length > 20 ? `${ex.error.slice(0, 17)}...` : ex.error
          : "";
        const prefix = isSelected ? "> " : "  ";

        return (
          <box key={ex.execution_id} height={1} width="100%">
            <text>
              {`${prefix}${badge.padEnd(5)}  ${formatTimestamp(ex.started_at).padEnd(19)}  ${formatDuration(ex.duration_ms).padEnd(8)}  ${trigger.padEnd(14)}  ${errorText}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
