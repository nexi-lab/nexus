/**
 * Reservation list with status badges, amounts, and purposes.
 */

import { For } from "solid-js";
import type { Reservation } from "../../stores/payments-store.js";

interface ReservationListProps {
  readonly reservations: readonly Reservation[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

const STATUS_BADGES: Readonly<Record<Reservation["status"], string>> = {
  pending: "◐",
  committed: "✓",
  released: "○",
};

function shortId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 8)}..`;
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return "n/a";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function ReservationList(props: ReservationListProps) {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading reservations..."
          : props.reservations.length === 0
            ? "No reservations yet. Reservations are created during transfers."
            : `${props.reservations.length} reservations`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {/* Header */}
        <box height={1} width="100%">
          <text>{"  ST  ID          AMOUNT       STATUS      PURPOSE              EXPIRES"}</text>
        </box>
        <box height={1} width="100%">
          <text>{"  --  ----------  -----------  ----------  -------------------  -------"}</text>
        </box>

        {/* Rows */}
        <For each={props.reservations}>{(r, i) => {
          const badge = STATUS_BADGES[r.status] ?? "?";
          const purpose = r.purpose.length > 19
            ? `${r.purpose.slice(0, 16)}...`
            : r.purpose;

          return (
            <box height={1} width="100%">
              <text>
                {`${i() === props.selectedIndex ? "> " : "  "}${badge}   ${shortId(r.id).padEnd(10)}  ${r.amount.padEnd(11)}  ${r.status.padEnd(10)}  ${purpose.padEnd(19)}  ${formatTimestamp(r.expires_at)}`}
              </text>
            </box>
          );
        }}</For>
      </scrollbox>
    </box>
  );
}
