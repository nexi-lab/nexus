/**
 * Reservation list with status badges, amounts, and purposes.
 */

import { For } from "solid-js";
import type { Reservation } from "../../stores/payments-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { EmptyState } from "../../shared/components/empty-state.js";

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
  if (props.loading) {
    return <LoadingIndicator message="Loading reservations..." />;
  }

  if (props.reservations.length === 0) {
    return <EmptyState message="No reservations yet." hint="Reservations are created during transfers." />;
  }

  return (
    <scrollbox height="100%" width="100%">
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
  );
}
