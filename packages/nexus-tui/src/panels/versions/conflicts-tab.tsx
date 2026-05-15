/**
 * Conflicts view for the Versions & Snapshots panel.
 * Displayed as a toggleable bottom pane (press 'c' to show/hide).
 */

import { For, Show } from "solid-js";
import type { ConflictItem } from "../../stores/versions-store.js";

interface ConflictsViewProps {
  readonly conflicts: readonly ConflictItem[];
  readonly loading: boolean;
  readonly visible: boolean;
}

export function ConflictsView(props: ConflictsViewProps) {
  return (
    <Show when={props.visible}>
      <Show
        when={!props.loading}
        fallback={
          <box height={6} width="100%" borderStyle="single" flexDirection="column">
            <text>{"--- Conflicts ---"}</text>
            <text>Loading conflicts...</text>
          </box>
        }
      >
        <Show
          when={props.conflicts.length > 0}
          fallback={
            <box height={4} width="100%" borderStyle="single" flexDirection="column">
              <text>{"--- Conflicts ---"}</text>
              <text>No conflicts detected.</text>
            </box>
          }
        >
          <box
            height={Math.min(props.conflicts.length + 3, 12)}
            width="100%"
            borderStyle="single"
            flexDirection="column"
          >
            <text>{"--- Conflicts ---"}</text>
            <scrollbox height="100%" width="100%">
              <For each={props.conflicts}>{(conflict, _i) => (
                <box height={1} width="100%">
                  <text>
                    {`  ${conflict.path}  ${conflict.reason}  expected:${conflict.expected_hash ?? "n/a"}  current:${conflict.current_hash ?? "n/a"}  txn:${conflict.transaction_id ?? "n/a"}`}
                  </text>
                </box>
              )}</For>
            </scrollbox>
          </box>
        </Show>
      </Show>
    </Show>
  );
}
