/**
 * Event replay tab with event type filtering.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import { createSignal, createEffect } from "solid-js";
import type { JSX } from "solid-js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useTextInput } from "../../shared/hooks/use-text-input.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { EventReplay } from "./event-replay.js";

interface ReplayTabProps {
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function ReplayTab(props: ReplayTabProps): JSX.Element {
  const client = useApi();
  const [typeFilter, setTypeFilter] = createSignal("");

  const fetchEventReplay = useKnowledgeStore.getState().fetchEventReplay;
  const clearEventReplay = useKnowledgeStore.getState().clearEventReplay;

  createEffect(() => {
    if (client) void fetchEventReplay({}, client);
  });

  const filterInput = useTextInput({
    onSubmit: (val) => {
      setTypeFilter(val);
      if (client) void fetchEventReplay({ event_types: val || undefined }, client);
    },
  });

  useKeyboard(
    (): Record<string, () => void> => {
      if (props.overlayActive) return {};
      if (filterInput.active) return filterInput.inputBindings;
      return {
        ...props.tabBindings,
        f: () => filterInput.activate(typeFilter()),
        r: () => {
          if (client) {
            clearEventReplay();
            void fetchEventReplay({ event_types: typeFilter() || undefined }, client);
          }
        },
      };
    },
    () => props.overlayActive ? undefined : filterInput.active ? filterInput.onUnhandled : undefined,
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>
          {filterInput.active
            ? `Filter event type: ${filterInput.buffer}\u2588`
            : `Filter: event_type=${typeFilter() || "*"}`}
        </text>
      </box>
      <box flexGrow={1} width="100%" borderStyle="single">
        <EventReplay typeFilter={typeFilter()} />
      </box>
      <box height={1} width="100%">
        <text>
          {filterInput.active
            ? "Type value, Enter:apply, Escape:cancel"
            : "f:filter event type  r:refresh  Tab:switch tab"}
        </text>
      </box>
    </box>
  );
}
