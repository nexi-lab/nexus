/**
 * Memories sub-tab for the Zones panel.
 * Displays registered memory directories with selection highlighting.
 */

import React from "react";
import type { MemoryInfo } from "../../stores/workspace-store.js";

interface MemoriesTabProps {
  readonly memories: readonly MemoryInfo[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

export function MemoriesTab({
  memories,
  selectedIndex,
  loading,
}: MemoriesTabProps): React.ReactNode {
  if (loading) return <text>Loading memories...</text>;
  if (memories.length === 0)
    return <text>No memory directories registered. Press 'n' to register one.</text>;

  return (
    <scrollbox height="100%" width="100%">
      {memories.map((mem, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        return (
          <box key={mem.path} height={1} width="100%">
            <text>{`${prefix}${mem.name}  ${mem.path}  ${mem.scope}  ${mem.created_by ?? ""}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
