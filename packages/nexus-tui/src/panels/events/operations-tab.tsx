import { Show, For } from "solid-js";
import type { JSX } from "solid-js";
/**
 * Operations tab: simple list of operations.
 *
 * Displays operation_id, agent_id, type, status, and started_at.
 */

import type { OperationItem } from "../../stores/infra-store.js";

interface OperationsTabProps {
  readonly operations: readonly OperationItem[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

export function OperationsTab(props: OperationsTabProps): JSX.Element {
  return (
    <Show
      when={!props.loading}
      fallback={<text>Loading operations...</text>}
    >
      <Show
        when={props.operations.length > 0}
        fallback={<text>No operations found.</text>}
      >
        <scrollbox height="100%" width="100%">
          {/* Header */}
          <box height={1} width="100%">
            <text>{"  OPERATION_ID       AGENT_ID         TYPE        STATUS      STARTED"}</text>
          </box>

          <For each={props.operations}>{(op, i) => {
            const isSelected = () => i() === props.selectedIndex;
            const prefix = () => isSelected() ? "> " : "  ";
            const opShort = op.operation_id.slice(0, 16) + "...";
            const agentShort = op.agent_id ? op.agent_id.slice(0, 14) : "n/a";
            const started = op.started_at ? op.started_at.slice(0, 19) : "n/a";
            return (
              <box height={1} width="100%">
                <text>{`${prefix()}${opShort}  ${agentShort.padEnd(16)}  ${op.type.padEnd(10)}  ${op.status.padEnd(10)}  ${started}`}</text>
              </box>
            );
          }}</For>
        </scrollbox>
      </Show>
    </Show>
  );
}
