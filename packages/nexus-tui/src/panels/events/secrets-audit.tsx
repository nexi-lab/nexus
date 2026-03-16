/**
 * Secrets audit log view: shows audit trail of secret access and modifications.
 */

import React from "react";
import type { SecretAuditEntry } from "../../stores/infra-store.js";
import { Spinner } from "../../shared/components/spinner.js";

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
        <text>{"  Event Type     Actor                Zone                 Time"}</text>
      </box>

      {entries.map((entry) => {
        const eventType = entry.event_type.padEnd(14).slice(0, 14);
        const actor = entry.actor_id.padEnd(20).slice(0, 20);
        const zone = entry.zone_id.padEnd(20).slice(0, 20);
        const time = entry.created_at.slice(11, 19);

        return (
          <box key={entry.id} height={1} width="100%">
            <text>{`  ${eventType} ${actor} ${zone} ${time}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
