/**
 * Secrets audit tab: secrets access log with filtering.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import React, { useState, useEffect } from "react";
import { useInfraStore } from "../../stores/infra-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useTextInput } from "../../shared/hooks/use-text-input.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { SecretsAudit } from "./secrets-audit.js";

interface SecretsTabProps {
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function SecretsTab({ tabBindings, overlayActive }: SecretsTabProps): React.ReactNode {
  const client = useApi();
  const [secretsFilter, setSecretsFilter] = useState("");

  const secretAuditEntries = useInfraStore((s) => s.secretAuditEntries);
  const secretsLoading = useInfraStore((s) => s.secretsLoading);
  const fetchSecretAudit = useInfraStore((s) => s.fetchSecretAudit);

  useEffect(() => {
    if (client) fetchSecretAudit(client);
  }, [client, fetchSecretAudit]);

  const filterInput = useTextInput({
    onSubmit: (val) => setSecretsFilter(val),
  });

  useKeyboard(
    overlayActive
      ? {}
      : filterInput.active
      ? filterInput.inputBindings
      : {
          ...tabBindings,
          "/": () => filterInput.activate(secretsFilter),
          r: () => { if (client) fetchSecretAudit(client); },
        },
    overlayActive ? undefined : filterInput.active ? filterInput.onUnhandled : undefined,
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>
          {filterInput.active
            ? `Filter: ${filterInput.buffer}\u2588`
            : secretsFilter
              ? `Filter: ${secretsFilter}`
              : ""}
        </text>
      </box>
      <box flexGrow={1} width="100%" borderStyle="single">
        <SecretsAudit
          entries={secretAuditEntries}
          loading={secretsLoading}
          filter={secretsFilter}
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
