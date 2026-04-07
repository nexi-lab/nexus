import type { JSX } from "solid-js";
/**
 * Workflow list view: name, version, enabled, triggers count, actions count, description.
 */

import type { WorkflowSummary } from "../../stores/workflows-store.js";
import { textStyle } from "../../shared/text-style.js";
import { statusColor } from "../../shared/theme.js";
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

export function WorkflowList(props: WorkflowListProps): JSX.Element {
  const renderWorkflow = (w: WorkflowSummary, i: number) => {
    const isSelected = i === props.selectedIndex;
    const enabledBadge = w.enabled ? "[ON]" : "[--]";
    const name = truncate(w.name, 19);
    const version = truncate(w.version, 8);
    const desc = w.description ? truncate(w.description, 30) : "";
    const prefix = isSelected ? "> " : "  ";

    return (
      <box height={1} width="100%">
        <text>
          <span>{prefix}</span>
          <span style={textStyle({ fg: w.enabled ? statusColor.healthy : statusColor.dim })}>{enabledBadge.padEnd(5)}</span>
          <span>{`${name.padEnd(19)}  ${version.padEnd(8)}  ${String(w.triggers).padEnd(4)}  ${String(w.actions).padEnd(3)}  ${desc}`}</span>
        </text>
      </box>
    );
  };

  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading workflows..."
          : props.workflows.length === 0
            ? "No workflows defined. Create one via the API: POST /api/v2/workflows"
            : `${props.workflows.length} workflows`}
      </text>

      {/* Header */}
      <box height={1} width="100%">
        <text>{"  EN   NAME                 VERSION   TRIG  ACT  DESCRIPTION"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ---  -------------------  --------  ----  ---  -----------"}</text>
      </box>

      {/* Rows */}
      <VirtualList
        items={props.workflows}
        renderItem={renderWorkflow}
        viewportHeight={VIEWPORT_HEIGHT}
        selectedIndex={props.selectedIndex}
      />
    </box>
  );
}
