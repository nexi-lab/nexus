/**
 * Secrets audit tab: secrets access log with filtering.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import { createSignal, createEffect, onCleanup } from "solid-js";
import type { JSX } from "solid-js";
import { useInfraStore } from "../../stores/infra-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useTextInput } from "../../shared/hooks/use-text-input.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { SecretsAudit } from "./secrets-audit.js";

interface SecretsTabProps {
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function SecretsTab(props: SecretsTabProps): JSX.Element {
  const client = useApi();
  const [secretsFilter, setSecretsFilter] = createSignal("");

  const [_rev, _setRev] = createSignal(0);
  const unsub = useInfraStore.subscribe(() => _setRev((r) => r + 1));
  onCleanup(unsub);
  const inf = () => { void _rev(); return useInfraStore.getState(); };

  const secretAuditEntries = () => inf().secretAuditEntries;
  const secretsLoading = () => inf().secretsLoading;
  const fetchSecretAudit = useInfraStore.getState().fetchSecretAudit;

  createEffect(() => {
    if (client) fetchSecretAudit(client);
  });

  const filterInput = useTextInput({
    onSubmit: (val) => setSecretsFilter(val),
  });

  useKeyboard(
    (): Record<string, () => void> => {
      if (props.overlayActive) return {};
      if (filterInput.active) return filterInput.inputBindings;
      return {
        ...props.tabBindings,
        "/": () => filterInput.activate(secretsFilter()),
        r: () => { if (client) fetchSecretAudit(client); },
      };
    },
    () => props.overlayActive ? undefined : filterInput.active ? filterInput.onUnhandled : undefined,
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>
          {filterInput.active
            ? `Filter: ${filterInput.buffer}\u2588`
            : secretsFilter()
              ? `Filter: ${secretsFilter()}`
              : ""}
        </text>
      </box>
      <box flexGrow={1} width="100%" borderStyle="single">
        <SecretsAudit
          entries={secretAuditEntries()}
          loading={secretsLoading()}
          filter={secretsFilter()}
        />
      </box>
      <box height={1} width="100%">
        <text>
          {filterInput.active
            ? "Type filter, Enter:apply, Escape:cancel"
            : "/:filter  r:refresh  Tab:switch tab"}
        </text>
      </box>
    </box>
  );
}
