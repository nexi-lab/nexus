/**
 * Workflow list view: name, status, trigger type, step count, last run.
 */

import React from "react";
import type { Workflow } from "../../stores/workflows-store.js";

interface WorkflowListProps {
  readonly workflows: readonly Workflow[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

const STATUS_BADGES: Readonly<Record<Workflow["status"], string>> = {
  active: "[ACT]",
  paused: "[PAU]",
  draft: "[DRF]",
  archived: "[ARC]",
};

function formatTimestamp(ts: string | null): string {
  if (!ts) return "never";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function WorkflowList({
  workflows,
  selectedIndex,
  loading,
}: WorkflowListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading workflows...</text>
      </box>
    );
  }

  if (workflows.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No workflows found</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  ST     NAME                 TRIGGER         STEPS  LAST RUN"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  -----  -------------------  --------------  -----  --------"}</text>
      </box>

      {/* Rows */}
      {workflows.map((w, i) => {
        const isSelected = i === selectedIndex;
        const badge = STATUS_BADGES[w.status] ?? `[${w.status.toUpperCase()}]`;
        const name = w.name.length > 19 ? `${w.name.slice(0, 16)}...` : w.name;
        const trigger = w.trigger_type.length > 14
          ? `${w.trigger_type.slice(0, 11)}...`
          : w.trigger_type;
        const prefix = isSelected ? "> " : "  ";

        return (
          <box key={w.workflow_id} height={1} width="100%">
            <text>
              {`${prefix}${badge.padEnd(5)}  ${name.padEnd(19)}  ${trigger.padEnd(14)}  ${String(w.step_count).padEnd(5)}  ${formatTimestamp(w.last_run)}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
