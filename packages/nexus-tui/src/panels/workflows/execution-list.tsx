import type { JSX } from "solid-js";
/**
 * Execution list view: execution_id, trigger_type, status, started_at,
 * completed_at, actions progress, error_message.
 */

import type { ExecutionSummary } from "../../stores/workflows-store.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { VirtualList } from "../../shared/components/virtual-list.js";

const VIEWPORT_HEIGHT = 20;

interface ExecutionListProps {
  readonly executions: readonly ExecutionSummary[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return "---";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function shortId(id: string): string {
  if (id.length <= 10) return id;
  return `${id.slice(0, 8)}..`;
}

function truncate(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen - 3)}...`;
}

export function ExecutionList({
  executions,
  selectedIndex,
  loading,
}: ExecutionListProps): JSX.Element {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading executions...</text>
      </box>
    );
  }

  if (executions.length === 0) {
    return (
      <EmptyState
        message="No executions found."
        hint="Select a workflow and press e to execute it."
      />
    );
  }

  const renderExecution = (ex: ExecutionSummary, i: number) => {
      const isSelected = i === selectedIndex;
      const id = shortId(ex.execution_id);
      const status = truncate(ex.status, 9);
      const trigger = truncate(ex.trigger_type, 12);
      const progress = `${ex.actions_completed}/${ex.actions_total}`;
      const errorText = ex.error_message
        ? truncate(ex.error_message, 20)
        : "";
      const prefix = isSelected ? "> " : "  ";

      return (
        <box height={1} width="100%">
          <text>
            {`${prefix}${id.padEnd(10)}  ${status.padEnd(9)}  ${trigger.padEnd(12)}  ${progress.padEnd(8)}  ${formatTimestamp(ex.started_at).padEnd(19)}  ${errorText}`}
          </text>
        </box>
      );
    };

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  ID          STATUS     TRIGGER       PROGRESS  STARTED              ERROR"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ----------  ---------  ------------  --------  -------------------  -----"}</text>
      </box>

      {/* Rows */}
      <VirtualList
        items={executions}
        renderItem={renderExecution}
        viewportHeight={VIEWPORT_HEIGHT}
        selectedIndex={selectedIndex}
      />
    </box>
  );
}
