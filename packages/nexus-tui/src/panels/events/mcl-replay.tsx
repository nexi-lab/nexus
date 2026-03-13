/**
 * MCL (Metadata Change Log) replay sub-view.
 * Shows structured change log entries from the operation log.
 * Supports client-side filtering by entity URN and aspect name.
 * Issue #2930.
 */

import React, { useEffect } from "react";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { useApi } from "../../shared/hooks/use-api.js";

export interface MclReplayProps {
  /** Optional URN filter string (substring match). */
  readonly urnFilter?: string;
  /** Optional aspect name filter string (substring match). */
  readonly aspectFilter?: string;
}

export function MclReplay(props: MclReplayProps): React.ReactNode {
  const client = useApi();
  const entries = useKnowledgeStore((s) => s.replayEntries);
  const loading = useKnowledgeStore((s) => s.replayLoading);
  const hasMore = useKnowledgeStore((s) => s.replayHasMore);
  const fetchReplay = useKnowledgeStore((s) => s.fetchReplay);
  const clearReplay = useKnowledgeStore((s) => s.clearReplay);
  const error = useKnowledgeStore((s) => s.error);

  const urnFilter = props.urnFilter ?? "";
  const aspectFilter = props.aspectFilter ?? "";

  // Re-fetch from server when filters change (or on initial mount)
  useEffect(() => {
    if (client) {
      clearReplay();
      void fetchReplay(client, 0, 200, urnFilter || undefined, aspectFilter || undefined);
    }
  }, [client, urnFilter, aspectFilter, fetchReplay, clearReplay]);

  // Apply filters client-side
  const filtered = entries.filter((e) => {
    if (urnFilter && !e.entityUrn.includes(urnFilter)) return false;
    if (aspectFilter && !e.aspectName.includes(aspectFilter)) return false;
    return true;
  });

  if (loading && entries.length === 0) {
    return <text>Loading MCL entries...</text>;
  }

  if (error) {
    return <text>{`Error: ${error}`}</text>;
  }

  if (entries.length === 0) {
    return <text>No MCL records found</text>;
  }

  return (
    <box flexDirection="column" height="100%" width="100%">
      {/* Filter bar */}
      <box height={1} width="100%">
        <text>
          {`  Filters: URN=${urnFilter || "*"}  Aspect=${aspectFilter || "*"}  (${filtered.length}/${entries.length} shown)`}
        </text>
      </box>
      <text>
        {"  Seq  Change       Aspect               Entity URN"}
      </text>
      {filtered.slice(0, 100).map((e) => (
        <text key={e.sequenceNumber}>
          {`  ${String(e.sequenceNumber).padStart(5)}  ${e.changeType.padEnd(12)} ${e.aspectName.padEnd(20)} ${e.entityUrn}`}
        </text>
      ))}
      {hasMore && <text>{"  ... more records available"}</text>}
    </box>
  );
}
