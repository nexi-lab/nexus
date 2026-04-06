import type { JSX } from "solid-js";
/**
 * Zone list view: shows zones from GET /api/zones.
 *
 * Displays: zone_id, name, domain, phase, is_active, created_at.
 */

import type { ZoneResponse } from "../../stores/zones-store.js";
import { EmptyState } from "../../shared/components/empty-state.js";

interface ZoneListProps {
  readonly zones: readonly ZoneResponse[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function truncate(value: string, maxLen: number): string {
  if (value.length <= maxLen) return value;
  return `${value.slice(0, maxLen - 2)}..`;
}

export function ZoneList(props: ZoneListProps): JSX.Element {
  // Unconditional rendering — ternary instead of if/return (evaluates once in Match)
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>{props.loading ? "Loading zones..." : props.zones.length === 0 ? "No zones found. Press n to create a zone." : ""}</text>
      <scrollbox flexGrow={1} width="100%">
        {/* Header */}
        <box height={1} width="100%">
          <text>{"  ZONE ID            NAME             DOMAIN           PHASE     ACTIVE  CREATED"}</text>
        </box>
        <box height={1} width="100%">
          <text>{"  -----------------  ---------------  ---------------  --------  ------  -------------------------"}</text>
        </box>

        {/* Rows */}
        {props.zones.map((zone, i) => {
          const isSelected = i === props.selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const activeLabel = zone.is_active ? "yes" : "no";

        return (
          <box height={1} width="100%">
            <text>
              {`${prefix}${truncate(zone.zone_id, 17).padEnd(17)}  ${truncate(zone.name, 15).padEnd(15)}  ${truncate(zone.domain ?? "-", 15).padEnd(15)}  ${zone.phase.padEnd(8)}  ${activeLabel.padEnd(6)}  ${formatTimestamp(zone.created_at)}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
    </box>
  );
}
