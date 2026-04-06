import { Show, For, createSignal, onCleanup } from "solid-js";
import type { JSX } from "solid-js";
/**
 * Historical event replay sub-view.
 *
 * Shows events from GET /api/v2/events/replay with filtering by
 * event type, path pattern, agent ID, and since timestamp.
 */


import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { Spinner } from "../../shared/components/spinner.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { textStyle } from "../../shared/text-style.js";
import { formatTimestamp } from "../../shared/utils/format-time.js";

export interface EventReplayProps {
  /** Optional event type filter (substring match, client-side). */
  readonly typeFilter?: string;
}

export function EventReplay(props: EventReplayProps): JSX.Element {
  const [_kRev, _setKRev] = createSignal(0);
  const unsub = useKnowledgeStore.subscribe(() => _setKRev((r) => r + 1));
  onCleanup(unsub);
  const ks = () => { void _kRev(); return useKnowledgeStore.getState(); };

  const entries = () => ks().eventReplayEntries;
  const loading = () => ks().eventReplayLoading;
  const hasMore = () => ks().eventReplayHasMore;
  const error = () => ks().error;

  const needle = () => (props.typeFilter ?? "").toLowerCase();
  const filtered = () => {
    const n = needle();
    return n
      ? entries().filter((e) => e.event_type.toLowerCase().includes(n))
      : entries();
  };

  return (
    <Show
      when={!(loading() && entries().length === 0)}
      fallback={<Spinner label="Loading event replay..." />}
    >
      <Show when={!error()} fallback={<text>{`Error: ${error()}`}</text>}>
        <Show
          when={entries().length > 0}
          fallback={
            <EmptyState
              message="No historical events found."
              hint="Adjust filters or wait for events to be recorded."
            />
          }
        >
          <box flexDirection="column" height="100%" width="100%">
            {/* Summary */}
            <box height={1} width="100%">
              <text style={textStyle({ dim: true })}>
                {`  ${filtered().length} event${filtered().length !== 1 ? "s" : ""}${needle() ? ` matching "${props.typeFilter}"` : ""}`}
              </text>
            </box>

            {/* Header */}
            <box height={1} width="100%">
              <text>{"  Event Type         Agent            Path                          Time"}</text>
            </box>

            {/* Rows */}
            <scrollbox flexGrow={1} width="100%">
              <For each={filtered().slice(0, 200)}>{(e) => {
                const eventType = e.event_type.padEnd(20).slice(0, 20);
                const agent = (e.agent_id ?? "\u2014").padEnd(16).slice(0, 16);
                const path = (e.path ?? "\u2014").padEnd(30).slice(0, 30);
                const time = formatTimestamp(e.timestamp);
                return (
                  <box height={1} width="100%">
                    <text>{`  ${eventType} ${agent} ${path} ${time}`}</text>
                  </box>
                );
              }}</For>
              <Show when={hasMore()}>
                <text style={textStyle({ dim: true })}>{"  ... more events available"}</text>
              </Show>
            </scrollbox>
          </box>
        </Show>
      </Show>
    </Show>
  );
}
