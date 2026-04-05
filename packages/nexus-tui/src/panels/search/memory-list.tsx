/**
 * Memory list: displays memory dicts from search results.
 *
 * When a memory has version history loaded, an expanded section
 * appears below that memory row showing each version.
 */

import React from "react";
import type { Memory, MemoryHistory, MemoryDiff } from "../../stores/search-store.js";
import { truncateText } from "../../shared/utils/format-text.js";

interface MemoryListProps {
  readonly memories: readonly Memory[];
  readonly selectedIndex: number;
  readonly loading: boolean;
  readonly memoryHistory: MemoryHistory | null;
  readonly memoryHistoryLoading: boolean;
  readonly memoryDiff: MemoryDiff | null;
  readonly memoryDiffLoading: boolean;
}

function shortId(id: unknown): string {
  const str = String(id ?? "");
  if (str.length <= 12) return str;
  return `${str.slice(0, 8)}..`;
}

function getMemoryField(memory: Memory, field: string): unknown {
  return (memory as Record<string, unknown>)[field];
}

function formatTimestamp(ts: string): string {
  if (!ts) return "";
  // Show date and time, truncated for terminal width
  return ts.slice(0, 19).replace("T", " ");
}

function VersionHistorySection({
  history,
  historyLoading,
}: {
  readonly history: MemoryHistory | null;
  readonly historyLoading: boolean;
}): React.ReactNode {
  if (historyLoading) {
    return (
      <box height={1} width="100%" marginLeft={4}>
        <text>{"Loading version history..."}</text>
      </box>
    );
  }

  if (!history) return null;

  return (
    <box width="100%" flexDirection="column" marginLeft={4}>
      <box height={1} width="100%">
        <text>{`Version History (current: v${history.current_version})`}</text>
      </box>
      <box height={1} width="100%">
        <text>{"    VER  STATUS      CREATED AT"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"    ---  ----------  -------------------"}</text>
      </box>
      {history.versions.map((v) => {
        const isCurrent = v.version === history.current_version;
        const marker = isCurrent ? " *" : "  ";
        const ver = String(v.version).padStart(3);
        const status = v.status.padEnd(10);
        const created = formatTimestamp(v.created_at);

        return (
          <box key={v.version} height={1} width="100%">
            <text>{`  ${marker}${ver}  ${status}  ${created}`}</text>
          </box>
        );
      })}
    </box>
  );
}

function DiffSection({
  diff,
  diffLoading,
}: {
  readonly diff: MemoryDiff | null;
  readonly diffLoading: boolean;
}): React.ReactNode {
  if (diffLoading) {
    return (
      <box height={1} width="100%" marginLeft={4}>
        <text>{"Loading diff..."}</text>
      </box>
    );
  }

  if (!diff) return null;

  return (
    <box width="100%" height={10} flexDirection="column" marginLeft={2}>
      <box height={1} width="100%">
        <text>{`Diff v${diff.v1} → v${diff.v2} (${diff.mode})`}</text>
      </box>
      <scrollbox flexGrow={1} width="100%">
        <diff diff={diff.diff} showLineNumbers />
      </scrollbox>
    </box>
  );
}

export function MemoryList({
  memories,
  selectedIndex,
  loading,
  memoryHistory,
  memoryHistoryLoading,
  memoryDiff,
  memoryDiffLoading,
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

  // Determine which memory has history expanded (matches memoryHistory.memory_id)
  const expandedMemoryId = memoryHistory?.memory_id ?? null;

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
          const memId = String(getMemoryField(m, "memory_id") ?? "");
          const hasHistory = memId === expandedMemoryId;
          const historyIndicator = hasHistory ? " [H]" : "";
          const prefix = isSelected ? "> " : "  ";
          const memoryIdDisplay = shortId(getMemoryField(m, "memory_id")).padEnd(12);
          const memType = String(getMemoryField(m, "type") ?? "unknown").padEnd(9);
          const content = truncateText(
            String(getMemoryField(m, "content") ?? JSON.stringify(m)),
            44,
          );

          return (
            <React.Fragment key={i}>
              <box height={1} width="100%">
                <text>
                  {`${prefix}${memoryIdDisplay}  ${memType}  ${content}${historyIndicator}`}
                </text>
              </box>
              {isSelected && (hasHistory || memoryHistoryLoading) && (
                <VersionHistorySection
                  history={memoryHistory}
                  historyLoading={memoryHistoryLoading}
                />
              )}
              {isSelected && (memoryDiff !== null || memoryDiffLoading) && (
                <DiffSection
                  diff={memoryDiff}
                  diffLoading={memoryDiffLoading}
                />
              )}
            </React.Fragment>
          );
        })}
      </scrollbox>
    </box>
  );
}
