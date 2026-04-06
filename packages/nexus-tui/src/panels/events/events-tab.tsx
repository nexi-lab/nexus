/**
 * Events SSE stream tab: live event stream with connection states,
 * filtering, expansion, and copy support.
 *
 * Extracted from events-panel.tsx (Issue 2A: split into per-tab sub-panels).
 */

import { createSignal, createEffect, Show, For } from "solid-js";
import type { JSX } from "solid-js";
import { useEventsStore } from "../../stores/events-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import { useTextInput } from "../../shared/hooks/use-text-input.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { statusColor } from "../../shared/theme.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { ScrollIndicator } from "../../shared/components/scroll-indicator.js";

function formatEventData(data: string): string {
  try {
    const parsed = JSON.parse(data);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return data;
  }
}

const HELP_NORMAL = "j/k:navigate  Enter:expand  f:filter type  s:search  c:clear  r:reconnect  y:copy  Tab:switch";
const HELP_INPUT = "Type value, Enter:apply, Escape:cancel, Backspace:delete";

interface EventsTabProps {
  /** Tab-level keybindings (tab cycling) to merge. */
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function EventsTab(props: EventsTabProps): JSX.Element {
  // Reactive store accessors (direct reads via jsx:preserve)
  const config = () => useGlobalStore((s) => s.config);

  const connected = () => useEventsStore((s) => s.connected);
  const events = () => useEventsStore((s) => s.filteredEvents);
  const reconnectCount = () => useEventsStore((s) => s.reconnectCount);
  const reconnectExhausted = () => useEventsStore((s) => s.reconnectExhausted);
  const filters = () => useEventsStore((s) => s.filters);
  const eventsOverflowed = () => useEventsStore((s) => s.eventsOverflowed);
  const evictedCount = () => useEventsStore((s) => s.evictedCount);
  const eventsBuffer = () => useEventsStore((s) => s.eventsBuffer);
  const connect = useEventsStore((s) => s.connect);
  const disconnect = useEventsStore((s) => s.disconnect);
  const clearEvents = useEventsStore((s) => s.clearEvents);
  const setFilter = useEventsStore((s) => s.setFilter);

  const { copy, copied } = useCopy();

  // Selection and expansion
  const [selectedEventIndex, setSelectedEventIndex] = createSignal(-1);
  const [expandedEventIndex, setExpandedEventIndex] = createSignal<number | null>(null);

  // Reset expanded event when events change (index may become stale)
  createEffect(() => {
    void events().length;
    setExpandedEventIndex(null);
  });

  // SSE connection is managed by the parent EventsPanel — no duplicate connect here.

  // Text inputs for type and search filters
  const typeFilter = useTextInput({
    onSubmit: (val) => setFilter({ eventType: val || null }),
  });
  const searchFilter = useTextInput({
    onSubmit: (val) => setFilter({ search: val || null }),
  });

  // List navigation
  const listNav = listNavigationBindings({
    getIndex: () => selectedEventIndex(),
    setIndex: (i) => setSelectedEventIndex(i),
    getLength: () => events().length,
  });

  useKeyboard(
    (): Record<string, () => void> => {
      if (props.overlayActive) return {};
      const anyFilterActive = typeFilter.active || searchFilter.active;
      if (anyFilterActive) {
        return typeFilter.active ? typeFilter.inputBindings : searchFilter.inputBindings;
      }
      const evts = events();
      const f = filters();
      const cfg = config();
      return {
        ...listNav,
        ...props.tabBindings,
        return: () => {
          if (selectedEventIndex() >= 0 && selectedEventIndex() < evts.length) {
            setExpandedEventIndex((prev) => prev === selectedEventIndex() ? null : selectedEventIndex());
          }
        },
        escape: () => {
          if (expandedEventIndex() !== null) setExpandedEventIndex(null);
        },
        c: () => clearEvents(),
        r: () => {
          if (cfg.apiKey && cfg.baseUrl) {
            disconnect();
            connect(cfg.baseUrl, cfg.apiKey, {
              agentId: cfg.agentId,
              subject: cfg.subject,
              zoneId: cfg.zoneId,
            });
          }
        },
        f: () => typeFilter.activate(f.eventType ?? ""),
        s: () => searchFilter.activate(f.search ?? ""),
        y: () => {
          const idx = selectedEventIndex() >= 0 ? selectedEventIndex() : evts.length - 1;
          const event = evts[idx];
          if (event) copy(event.data);
        },
      };
    },
    () => {
      if (props.overlayActive) return undefined;
      const anyFilterActive = typeFilter.active || searchFilter.active;
      return anyFilterActive
        ? (typeFilter.active ? typeFilter.onUnhandled : searchFilter.onUnhandled)
        : undefined;
    },
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Filter bar */}
      <box height={1} width="100%">
        <text>
          {typeFilter.active
            ? `Filter type: ${typeFilter.buffer}\u2588`
            : searchFilter.active
              ? `Filter search: ${searchFilter.buffer}\u2588`
              : `Filter: type=${filters().eventType ?? "*"} search=${filters().search ?? "*"}`}
        </text>
      </box>

      {/* Main content */}
      <box flexGrow={1} width="100%" borderStyle="single">
        <box height="100%" width="100%" flexDirection="column">
          {/* SSE status */}
          <box height={1} width="100%">
            <text>
              {connected()
                ? `● Connected — ${events().length} events`
                : reconnectExhausted()
                  ? `✕ Reconnect failed after ${reconnectCount()} attempts — press r to retry`
                  : reconnectCount() > 0
                    ? `◐ Auto-reconnecting (attempt ${reconnectCount()}/10)...`
                    : "○ Disconnected"}
            </text>
          </box>

          {/* Overflow indicator */}
          <Show when={eventsOverflowed()}>
            <box height={1} width="100%">
              <text dimColor>
                {`Showing latest ${eventsBuffer().size} of ${eventsBuffer().totalAdded} events (${evictedCount()} evicted)`}
              </text>
            </box>
          </Show>

          {/* Event stream */}
          <Show
            when={expandedEventIndex() !== null && expandedEventIndex()! < events().length}
            fallback={
              <ScrollIndicator selectedIndex={selectedEventIndex() >= 0 ? selectedEventIndex() : events().length - 1} totalItems={events().length} visibleItems={20}>
                <scrollbox flexGrow={1} width="100%">
                  <Show
                    when={events().length > 0}
                    fallback={
                      <EmptyState
                        message="Listening for events..."
                        hint="Waiting for activity on the server."
                      />
                    }
                  >
                    <For each={events()}>{(event, index) => (
                      <box height={1} width="100%" flexDirection="row">
                        <text inverse={index() === selectedEventIndex() || undefined}>
                          {`[${event.event}] ${event.data}`}
                        </text>
                      </box>
                    )}</For>
                  </Show>
                </scrollbox>
              </ScrollIndicator>
            }
          >
            <box flexGrow={1} width="100%" flexDirection="column">
              <box height={1} width="100%">
                <text bold>{`[${events()[expandedEventIndex()!]!.event}] — Event #${expandedEventIndex()} (Escape to close)`}</text>
              </box>
              <scrollbox flexGrow={1} width="100%">
                <text>{formatEventData(events()[expandedEventIndex()!]!.data)}</text>
              </scrollbox>
            </box>
          </Show>
        </box>
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <Show
          when={!copied}
          fallback={<text foregroundColor={statusColor.success}>Copied!</text>}
        >
          <text>{(typeFilter.active || searchFilter.active) ? HELP_INPUT : HELP_NORMAL}</text>
        </Show>
      </box>
    </box>
  );
}
