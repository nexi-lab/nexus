import { For } from "solid-js";
import type { JSX } from "solid-js";
/**
 * Secrets audit log view: shows audit trail of secret access and modifications.
 */

import type { SecretAuditEntry } from "../../stores/infra-store.js";
import { textStyle } from "../../shared/text-style.js";
import { formatTimestamp } from "../../shared/utils/format-time.js";

export function SecretsAudit(props: {
  readonly entries: readonly SecretAuditEntry[];
  readonly loading: boolean;
  readonly filter?: string;
}): JSX.Element {
  const needle = () => (props.filter ?? "").toLowerCase();
  const filtered = () => {
    const n = needle();
    return n
      ? props.entries.filter((e) => {
          const haystack = `${e.event_type} ${e.actor_id} ${e.details ?? ""}`.toLowerCase();
          return haystack.includes(n);
        })
      : props.entries;
  };

  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading secrets audit..."
          : props.entries.length === 0
            ? "No audit entries"
            : `${props.entries.length} audit entries`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {/* Count indicator */}
        {needle() && (
          <box height={1} width="100%">
            <text style={textStyle({ dim: true })}>{`${filtered().length} of ${props.entries.length} entries`}</text>
          </box>
        )}

        {/* Header */}
        <box height={1} width="100%">
          <text>{"  Event Type     Actor                Zone                 Time"}</text>
        </box>

        <For each={filtered()}>{(entry) => {
          const eventType = entry.event_type.padEnd(14).slice(0, 14);
          const actor = entry.actor_id.padEnd(20).slice(0, 20);
          const zone = entry.zone_id.padEnd(20).slice(0, 20);
          const time = formatTimestamp(entry.created_at);

          return (
            <box height={1} width="100%">
              <text>{`  ${eventType} ${actor} ${zone} ${time}`}</text>
            </box>
          );
        }}</For>
      </scrollbox>
    </box>
  );
}
