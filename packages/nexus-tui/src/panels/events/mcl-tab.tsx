/**
 * MCL (MetaCatalog Language) replay tab with URN/aspect filtering.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import React, { useState, useEffect } from "react";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useTextInput } from "../../shared/hooks/use-text-input.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { MclReplay } from "./mcl-replay.js";

interface MclTabProps {
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function MclTab({ tabBindings, overlayActive }: MclTabProps): React.ReactNode {
  const client = useApi();
  const [urnFilter, setUrnFilter] = useState("");
  const [aspectFilter, setAspectFilter] = useState("");

  const fetchReplay = useKnowledgeStore((s) => s.fetchReplay);
  const clearReplay = useKnowledgeStore((s) => s.clearReplay);

  useEffect(() => {
    if (client) void fetchReplay(client, 0, 50);
  }, [client, fetchReplay]);

  const urnInput = useTextInput({
    onSubmit: (val) => setUrnFilter(val),
  });
  const aspectInput = useTextInput({
    onSubmit: (val) => setAspectFilter(val),
  });
  const anyInputActive = urnInput.active || aspectInput.active;

  useKeyboard(
    overlayActive
      ? {}
      : anyInputActive
      ? (urnInput.active ? urnInput.inputBindings : aspectInput.inputBindings)
      : {
          ...tabBindings,
          u: () => urnInput.activate(urnFilter),
          n: () => aspectInput.activate(aspectFilter),
          r: () => {
            if (client) {
              clearReplay();
              void fetchReplay(client, 0, 50);
            }
          },
        },
    overlayActive ? undefined : anyInputActive
      ? (urnInput.active ? urnInput.onUnhandled : aspectInput.onUnhandled)
      : undefined,
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>
          {urnInput.active
            ? `Filter URN: ${urnInput.buffer}\u2588`
            : aspectInput.active
              ? `Filter aspect: ${aspectInput.buffer}\u2588`
              : `Filter: URN=${urnFilter || "*"} aspect=${aspectFilter || "*"}`}
        </text>
      </box>
      <box flexGrow={1} width="100%" borderStyle="single">
        <MclReplay urnFilter={urnFilter} aspectFilter={aspectFilter} />
      </box>
      <box height={1} width="100%">
        <text>
          {anyInputActive
            ? "Type value, Enter:apply, Escape:cancel"
            : "u:filter URN  n:filter aspect  r:refresh  Tab:switch tab"}
        </text>
      </box>
    </box>
  );
}
