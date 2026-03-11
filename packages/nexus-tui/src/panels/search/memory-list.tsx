/**
 * Memory list: agent_id, type, content preview, tags, version.
 */

import React from "react";
import type { Memory } from "../../stores/search-store.js";

interface MemoryListProps {
  readonly memories: readonly Memory[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function truncateContent(content: string, maxLen: number): string {
  if (content.length <= maxLen) return content;
  return `${content.slice(0, maxLen - 3)}...`;
}

function formatTags(tags: readonly string[]): string {
  if (tags.length === 0) return "";
  return `[${tags.join(", ")}]`;
}

function shortId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 8)}..`;
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
        <text>{"  AGENT       TYPE       V  CONTENT                          TAGS"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ----------  ---------  -  -------------------------------  --------"}</text>
      </box>

      {/* Rows */}
      <scrollbox flexGrow={1} width="100%">
        {memories.map((m, i) => {
          const isSelected = i === selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const agent = shortId(m.agent_id).padEnd(10);
          const type = m.type.padEnd(9);
          const content = truncateContent(m.content, 31);
          const tags = formatTags(m.tags);

          return (
            <box key={m.memory_id} height={1} width="100%">
              <text>
                {`${prefix}${agent}  ${type}  ${String(m.version)}  ${content.padEnd(31)}  ${tags}`}
              </text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
