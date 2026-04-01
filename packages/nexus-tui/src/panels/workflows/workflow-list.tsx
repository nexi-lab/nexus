/**
 * Workflow list view: name, version, enabled, triggers count, actions count, description.
 */

import React, { useCallback } from "react";
import type { WorkflowSummary } from "../../stores/workflows-store.js";
import { textStyle } from "../../shared/text-style.js";
import { statusColor } from "../../shared/theme.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { VirtualList } from "../../shared/components/virtual-list.js";

const VIEWPORT_HEIGHT = 20;

interface WorkflowListProps {
  readonly workflows: readonly WorkflowSummary[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function truncate(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen - 3)}...`;
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
      <EmptyState
        message="No workflows defined."
        hint="Create one via the API: POST /api/v2/workflows"
      />
    );
  }

  const renderWorkflow = useCallback(
    (w: WorkflowSummary, i: number) => {
      const isSelected = i === selectedIndex;
      const enabledBadge = w.enabled ? "[ON]" : "[--]";
      const name = truncate(w.name, 19);
      const version = truncate(w.version, 8);
      const desc = w.description ? truncate(w.description, 30) : "";
      const prefix = isSelected ? "> " : "  ";

      return (
        <box key={w.name} height={1} width="100%">
          <text>
            <span>{prefix}</span>
            <span style={textStyle({ fg: w.enabled ? statusColor.healthy : statusColor.dim })}>{enabledBadge.padEnd(5)}</span>
            <span>{`${name.padEnd(19)}  ${version.padEnd(8)}  ${String(w.triggers).padEnd(4)}  ${String(w.actions).padEnd(3)}  ${desc}`}</span>
          </text>
        </box>
      );
    },
    [selectedIndex],
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  EN   NAME                 VERSION   TRIG  ACT  DESCRIPTION"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ---  -------------------  --------  ----  ---  -----------"}</text>
      </box>

      {/* Rows */}
      <VirtualList
        items={workflows}
        renderItem={renderWorkflow}
        viewportHeight={VIEWPORT_HEIGHT}
        selectedIndex={selectedIndex}
      />
    </box>
  );
}
