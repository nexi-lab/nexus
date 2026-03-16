/**
 * Secrets audit log view: shows audit trail of secret access and modifications.
 */

import React from "react";
import type { SecretAuditEntry } from "../../stores/infra-store.js";
import { Spinner } from "../../shared/components/spinner.js";
import { formatTimestamp } from "../../shared/utils/format-time.js";

export function SecretsAudit({
  entries,
  loading,
  filter,
}: {
  readonly entries: readonly SecretAuditEntry[];
  readonly loading: boolean;
  readonly filter?: string;
}): React.ReactNode {
  if (loading) {
    return <Spinner label="Loading secrets audit..." />;
  }

  if (entries.length === 0) {
    return <text>No audit entries</text>;
  }

  const needle = (filter ?? "").toLowerCase();
  const filtered = needle
    ? entries.filter((e) => {
        const haystack = `${e.event_type} ${e.actor_id} ${e.details ?? ""}`.toLowerCase();
        return haystack.includes(needle);
      })
    : entries;

  return (
    <scrollbox height="100%" width="100%">
      {/* Count indicator */}
      {needle ? (
        <box height={1} width="100%">
          <text dimColor>{`${filtered.length} of ${entries.length} entries`}</text>
        </box>
      ) : null}

      {/* Header */}
      <box height={1} width="100%">
        <text>{"  Event Type     Actor                Zone                 Time"}</text>
      </box>

      {filtered.map((entry) => {
        const eventType = entry.event_type.padEnd(14).slice(0, 14);
        const actor = entry.actor_id.padEnd(20).slice(0, 20);
        const zone = entry.zone_id.padEnd(20).slice(0, 20);
        const time = formatTimestamp(entry.created_at);

        return (
          <box key={entry.id} height={1} width="100%">
            <text>{`  ${eventType} ${actor} ${zone} ${time}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
