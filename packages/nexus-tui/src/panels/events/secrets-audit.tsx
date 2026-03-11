/**
 * Secrets audit log view: shows audit trail of secret access and modifications.
 */

import React from "react";
import type { SecretAuditEntry } from "../../stores/infra-store.js";
import { Spinner } from "../../shared/components/spinner.js";

const RESULT_ICON: Record<string, string> = {
  success: "✓",
  denied: "✗",
  error: "!",
};

export function SecretsAudit({
  entries,
  loading,
}: {
  readonly entries: readonly SecretAuditEntry[];
  readonly loading: boolean;
}): React.ReactNode {
  if (loading) {
    return <Spinner label="Loading secrets audit..." />;
  }

  if (entries.length === 0) {
    return <text>No audit entries</text>;
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  Result  Action       Secret               Actor                Time"}</text>
      </box>

      {entries.map((entry) => {
        const icon = RESULT_ICON[entry.result] ?? "?";
        const action = entry.action.padEnd(12).slice(0, 12);
        const secret = entry.secret_name.padEnd(20).slice(0, 20);
        const actor = entry.actor.padEnd(20).slice(0, 20);
        const time = entry.timestamp.slice(11, 19);

        return (
          <box key={entry.entry_id} height={1} width="100%">
            <text>{`  ${icon}     ${action} ${secret} ${actor} ${time}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
