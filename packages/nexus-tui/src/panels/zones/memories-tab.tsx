import type { JSX } from "solid-js";
/**
 * Memories sub-tab for the Zones panel.
 * Displays registered memory directories with selection highlighting.
 */

import type { MemoryInfo } from "../../stores/workspace-store.js";

interface MemoriesTabProps {
  readonly memories: readonly MemoryInfo[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

export function MemoriesTab(props: MemoriesTabProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading memories..."
          : props.memories.length === 0
            ? "No memory directories registered. Press 'n' to register one."
            : `${props.memories.length} memories`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {props.memories.map((mem, i) => {
          const isSelected = i === props.selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          return (
            <box height={1} width="100%">
              <text>{`${prefix}${mem.name}  ${mem.path}  ${mem.scope}  ${mem.created_by ?? ""}`}</text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
