/**
 * Memory list: displays memory dicts from search results.
 */

import React from "react";
import type { Memory } from "../../stores/search-store.js";

interface MemoryListProps {
  readonly memories: readonly Memory[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function truncateText(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen - 3)}...`;
}

function shortId(id: unknown): string {
  const str = String(id ?? "");
  if (str.length <= 12) return str;
  return `${str.slice(0, 8)}..`;
}

function getMemoryField(memory: Memory, field: string): unknown {
  return (memory as Record<string, unknown>)[field];
}

export function MemoryList({
  memories,
  selectedIndex,
  loading,
}: MemoryListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading memories...</text>
      </box>
    );
  }

  if (memories.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No memories found</text>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={1} width="100%">
        <text>{`Memories: ${memories.length}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ID            TYPE       CONTENT"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ------------  ---------  ------------------------------------------------"}</text>
      </box>

      {/* Rows */}
      <scrollbox flexGrow={1} width="100%">
        {memories.map((m, i) => {
          const isSelected = i === selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const memoryId = shortId(getMemoryField(m, "memory_id")).padEnd(12);
          const memType = String(getMemoryField(m, "type") ?? "unknown").padEnd(9);
          const content = truncateText(
            String(getMemoryField(m, "content") ?? JSON.stringify(m)),
            48,
          );

          return (
            <box key={i} height={1} width="100%">
              <text>
                {`${prefix}${memoryId}  ${memType}  ${content}`}
              </text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
