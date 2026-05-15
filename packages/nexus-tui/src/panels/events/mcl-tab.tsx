/**
 * MCL (MetaCatalog Language) replay tab with URN/aspect filtering.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import { createSignal, createEffect } from "solid-js";
import type { JSX } from "solid-js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useTextInput } from "../../shared/hooks/use-text-input.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { MclReplay } from "./mcl-replay.js";

interface MclTabProps {
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function MclTab(props: MclTabProps): JSX.Element {
  const client = useApi();
  const [urnFilter, setUrnFilter] = createSignal("");
  const [aspectFilter, setAspectFilter] = createSignal("");

  const fetchReplay = useKnowledgeStore.getState().fetchReplay;
  const clearReplay = useKnowledgeStore.getState().clearReplay;

  createEffect(() => {
    if (client) void fetchReplay(client, 0, 50);
  });

  const urnInput = useTextInput({
    onSubmit: (val) => setUrnFilter(val),
  });
  const aspectInput = useTextInput({
    onSubmit: (val) => setAspectFilter(val),
  });

  useKeyboard(
    (): Record<string, () => void> => {
      if (props.overlayActive) return {};
      const anyInputActive = urnInput.active || aspectInput.active;
      if (anyInputActive) {
        return urnInput.active ? urnInput.inputBindings : aspectInput.inputBindings;
      }
      return {
        ...props.tabBindings,
        u: () => urnInput.activate(urnFilter()),
        n: () => aspectInput.activate(aspectFilter()),
        r: () => {
          if (client) {
            clearReplay();
            void fetchReplay(client, 0, 50);
          }
        },
      };
    },
    () => {
      if (props.overlayActive) return undefined;
      const anyInputActive = urnInput.active || aspectInput.active;
      return anyInputActive
        ? (urnInput.active ? urnInput.onUnhandled : aspectInput.onUnhandled)
        : undefined;
    },
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>
          {urnInput.active
            ? `Filter URN: ${urnInput.buffer}\u2588`
            : aspectInput.active
              ? `Filter aspect: ${aspectInput.buffer}\u2588`
              : `Filter: URN=${urnFilter() || "*"} aspect=${aspectFilter() || "*"}`}
        </text>
      </box>
      <box flexGrow={1} width="100%" borderStyle="single">
        <MclReplay urnFilter={urnFilter()} aspectFilter={aspectFilter()} />
      </box>
      <box height={1} width="100%">
        <text>
          {(urnInput.active || aspectInput.active)
            ? "Type value, Enter:apply, Escape:cancel"
            : "u:filter URN  n:filter aspect  r:refresh  Tab:switch tab"}
        </text>
      </box>
    </box>
  );
}
