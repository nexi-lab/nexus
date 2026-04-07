import type { JSX } from "solid-js";
/**
 * Memory list: displays memory dicts from search results.
 *
 * When a memory has version history loaded, an expanded section
 * appears below that memory row showing each version.
 */

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

function VersionHistorySection(props: {
  readonly history: MemoryHistory | null;
  readonly historyLoading: boolean;
}): JSX.Element {
  return (
    <box width="100%" flexDirection="column" marginLeft={4}>
      <text>
        {props.historyLoading
          ? "Loading version history..."
          : !props.history
            ? ""
            : `Version History (current: v${props.history.current_version})`}
      </text>

      {(() => {
        if (props.historyLoading || !props.history) return null;
        return (
          <>
            <box height={1} width="100%">
              <text>{"    VER  STATUS      CREATED AT"}</text>
            </box>
            <box height={1} width="100%">
              <text>{"    ---  ----------  -------------------"}</text>
            </box>
            {props.history.versions.map((v) => {
              const isCurrent = v.version === props.history!.current_version;
              const marker = isCurrent ? " *" : "  ";
              const ver = String(v.version).padStart(3);
              const status = v.status.padEnd(10);
              const created = formatTimestamp(v.created_at);

              return (
                <box height={1} width="100%">
                  <text>{`  ${marker}${ver}  ${status}  ${created}`}</text>
                </box>
              );
            })}
          </>
        );
      })()}
    </box>
  );
}

function DiffSection(props: {
  readonly diff: MemoryDiff | null;
  readonly diffLoading: boolean;
}): JSX.Element {
  return (
    <box width="100%" flexDirection="column" marginLeft={2}>
      <text>
        {props.diffLoading
          ? "Loading diff..."
          : !props.diff
            ? ""
            : `Diff v${props.diff.v1} → v${props.diff.v2} (${props.diff.mode})`}
      </text>

      {(() => {
        if (props.diffLoading || !props.diff) return null;
        return (
          <scrollbox height={9} width="100%">
            <diff diff={props.diff.diff} showLineNumbers />
          </scrollbox>
        );
      })()}
    </box>
  );
}

export function MemoryList(props: MemoryListProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading memories..."
          : props.memories.length === 0
            ? "No memories found"
            : `Memories: ${props.memories.length}`}
      </text>

      {/* Header */}
      <box height={1} width="100%">
        <text>{"  ID            TYPE       CONTENT"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ------------  ---------  ------------------------------------------------"}</text>
      </box>

      {/* Rows */}
      <scrollbox flexGrow={1} width="100%">
        {props.memories.map((m, i) => {
          const isSelected = i === props.selectedIndex;
          const memId = String(getMemoryField(m, "memory_id") ?? "");
          const expandedMemoryId = props.memoryHistory?.memory_id ?? null;
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
            <>
              <box height={1} width="100%">
                <text>
                  {`${prefix}${memoryIdDisplay}  ${memType}  ${content}${historyIndicator}`}
                </text>
              </box>
              {isSelected && (hasHistory || props.memoryHistoryLoading) && (
                <VersionHistorySection
                  history={props.memoryHistory}
                  historyLoading={props.memoryHistoryLoading}
                />
              )}
              {isSelected && (props.memoryDiff !== null || props.memoryDiffLoading) && (
                <DiffSection
                  diff={props.memoryDiff}
                  diffLoading={props.memoryDiffLoading}
                />
              )}
            </>
          );
        })}
      </scrollbox>
    </box>
  );
}
