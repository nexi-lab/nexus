/**
 * Historical event replay sub-view.
 *
 * Shows events from GET /api/v2/events/replay with filtering by
 * event type, path pattern, agent ID, and since timestamp.
 */

import React from "react";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import type { EventReplayEntry } from "../../stores/knowledge-store.js";
import { Spinner } from "../../shared/components/spinner.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { textStyle } from "../../shared/text-style.js";
import { formatTimestamp } from "../../shared/utils/format-time.js";

export interface EventReplayProps {
  /** Optional event type filter (substring match, client-side). */
  readonly typeFilter?: string;
}

export function EventReplay({ typeFilter }: EventReplayProps): React.ReactNode {
  const entries = useKnowledgeStore((s) => s.eventReplayEntries);
  const loading = useKnowledgeStore((s) => s.eventReplayLoading);
  const hasMore = useKnowledgeStore((s) => s.eventReplayHasMore);
  const error = useKnowledgeStore((s) => s.error);

  const needle = (typeFilter ?? "").toLowerCase();
  const filtered: readonly EventReplayEntry[] = needle
    ? entries.filter((e) => e.event_type.toLowerCase().includes(needle))
    : entries;

  if (loading && entries.length === 0) {
    return <Spinner label="Loading event replay..." />;
  }

  if (error) {
    return <text>{`Error: ${error}`}</text>;
  }

  if (entries.length === 0) {
    return (
      <EmptyState
        message="No historical events found."
        hint="Adjust filters or wait for events to be recorded."
      />
    );
  }

  return (
    <box flexDirection="column" height="100%" width="100%">
      {/* Summary */}
      <box height={1} width="100%">
        <text style={textStyle({ dim: true })}>
          {`  ${filtered.length} event${filtered.length !== 1 ? "s" : ""}${needle ? ` matching "${typeFilter}"` : ""}`}
        </text>
      </box>

      {/* Header */}
      <box height={1} width="100%">
        <text>{"  Event Type         Agent            Path                          Time"}</text>
      </box>

      {/* Rows */}
      <scrollbox flexGrow={1} width="100%">
        {filtered.slice(0, 200).map((e) => {
          const eventType = e.event_type.padEnd(20).slice(0, 20);
          const agent = (e.agent_id ?? "—").padEnd(16).slice(0, 16);
          const path = (e.path ?? "—").padEnd(30).slice(0, 30);
          const time = formatTimestamp(e.timestamp);
          return (
            <box key={e.event_id} height={1} width="100%">
              <text>{`  ${eventType} ${agent} ${path} ${time}`}</text>
            </box>
          );
        })}
        {hasMore && <text style={textStyle({ dim: true })}>{"  ... more events available"}</text>}
      </scrollbox>
    </box>
  );
}
