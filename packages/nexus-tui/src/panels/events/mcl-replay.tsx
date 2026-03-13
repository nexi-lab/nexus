/**
 * MCL (Metadata Change Log) replay sub-view.
 * Shows structured change log entries from the operation log.
 * Issue #2930.
 */

import React, { useEffect } from "react";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { useApi } from "../../shared/hooks/use-api.js";

export function MclReplay(): React.ReactNode {
  const client = useApi();
  const entries = useKnowledgeStore((s) => s.replayEntries);
  const loading = useKnowledgeStore((s) => s.replayLoading);
  const hasMore = useKnowledgeStore((s) => s.replayHasMore);
  const fetchReplay = useKnowledgeStore((s) => s.fetchReplay);
  const error = useKnowledgeStore((s) => s.error);

  useEffect(() => {
    if (client && entries.length === 0) {
      void fetchReplay(client, 0, 50);
    }
  }, [client, entries.length, fetchReplay]);

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
      <text>
        {"  Seq  Change       Aspect               Entity URN"}
      </text>
      {entries.slice(0, 20).map((e) => (
        <text key={e.sequenceNumber}>
          {`  ${String(e.sequenceNumber).padStart(5)}  ${e.changeType.padEnd(12)} ${e.aspectName.padEnd(20)} ${e.entityUrn}`}
        </text>
      ))}
      {hasMore && <text>{"  ... more records available"}</text>}
    </box>
  );
}
