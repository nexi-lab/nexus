/**
 * MCL (Metadata Change Log) replay sub-view.
 * Shows structured change log entries from the operation log.
 * Supports client-side filtering by entity URN and aspect name.
 * Issue #2930.
 */

import { createEffect, Show, For } from "solid-js";
import type { JSX } from "solid-js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { useApi } from "../../shared/hooks/use-api.js";

export interface MclReplayProps {
  /** Optional URN filter string (substring match). */
  readonly urnFilter?: string;
  /** Optional aspect name filter string (substring match). */
  readonly aspectFilter?: string;
}

export function MclReplay(props: MclReplayProps): JSX.Element {
  const client = useApi();

  const entries = () => useKnowledgeStore((s) => s.replayEntries);
  const loading = () => useKnowledgeStore((s) => s.replayLoading);
  const hasMore = () => useKnowledgeStore((s) => s.replayHasMore);
  const error = () => useKnowledgeStore((s) => s.error);
  const fetchReplay = useKnowledgeStore((s) => s.fetchReplay);
  const clearReplay = useKnowledgeStore((s) => s.clearReplay);

  const urnFilter = () => props.urnFilter ?? "";
  const aspectFilter = () => props.aspectFilter ?? "";

  // Re-fetch from server when filters change (or on initial mount)
  createEffect(() => {
    const uf = urnFilter();
    const af = aspectFilter();
    if (client) {
      queueMicrotask(() => {
        clearReplay();
        void fetchReplay(client, 0, 200, uf || undefined, af || undefined);
      });
    }
  });

  // Apply filters client-side
  const filtered = () => {
    const uf = urnFilter();
    const af = aspectFilter();
    return entries().filter((e) => {
      if (uf && !e.entityUrn.includes(uf)) return false;
      if (af && !e.aspectName.includes(af)) return false;
      return true;
    });
  };

  return (
    <Show
      when={!(loading() && entries().length === 0)}
      fallback={<text>Loading MCL entries...</text>}
    >
      <Show when={!error()} fallback={<text>{`Error: ${error()}`}</text>}>
        <Show
          when={entries().length > 0}
          fallback={<text>No MCL records found</text>}
        >
          <box flexDirection="column" height="100%" width="100%">
            {/* Filter bar */}
            <box height={1} width="100%">
              <text>
                {`  Filters: URN=${urnFilter() || "*"}  Aspect=${aspectFilter() || "*"}  (${filtered().length}/${entries().length} shown)`}
              </text>
            </box>
            <text>
              {"  Seq  Change       Aspect               Entity URN"}
            </text>
            <For each={filtered().slice(0, 100)}>{(e) => (
              <text>
                {`  ${String(e.sequenceNumber).padStart(5)}  ${e.changeType.padEnd(12)} ${e.aspectName.padEnd(20)} ${e.entityUrn}`}
              </text>
            )}</For>
            <Show when={hasMore()}>
              <text>{"  ... more records available"}</text>
            </Show>
          </box>
        </Show>
      </Show>
    </Show>
  );
}
