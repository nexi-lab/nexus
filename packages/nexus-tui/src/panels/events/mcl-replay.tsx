/**
 * MCL (Metadata Change Log) replay sub-view.
 * Shows structured change log entries from the operation log.
 * Supports client-side filtering by entity URN and aspect name.
 * Issue #2930.
 */

import React, { useEffect, useState } from "react";
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
  const error = useKnowledgeStore((s) => s.error);

  const [urnFilter, setUrnFilter] = useState(props.urnFilter ?? "");
  const [aspectFilter, setAspectFilter] = useState(props.aspectFilter ?? "");

  // Sync from props when they change
  useEffect(() => {
    setUrnFilter(props.urnFilter ?? "");
  }, [props.urnFilter]);

  useEffect(() => {
    setAspectFilter(props.aspectFilter ?? "");
  }, [props.aspectFilter]);

  useEffect(() => {
    if (client && entries.length === 0) {
      void fetchReplay(client, 0, 50, urnFilter || undefined, aspectFilter || undefined);
    }
  }, [client, entries.length, fetchReplay, urnFilter, aspectFilter]);

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
      {filtered.slice(0, 20).map((e) => (
        <text key={e.sequenceNumber}>
          {`  ${String(e.sequenceNumber).padStart(5)}  ${e.changeType.padEnd(12)} ${e.aspectName.padEnd(20)} ${e.entityUrn}`}
        </text>
      ))}
      {hasMore && <text>{"  ... more records available"}</text>}
    </box>
  );
}
