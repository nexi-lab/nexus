/**
 * Reservation list with status badges, amounts, and purposes.
 */

import React from "react";
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

export function ReservationList({
  reservations,
  selectedIndex,
  loading,
}: ReservationListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading reservations...</text>
      </box>
    );
  }

  if (reservations.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No reservations found</text>
      </box>
    );
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
      {reservations.map((r, i) => {
        const isSelected = i === selectedIndex;
        const badge = STATUS_BADGES[r.status] ?? "?";
        const purpose = r.purpose.length > 19
          ? `${r.purpose.slice(0, 16)}...`
          : r.purpose;
        const prefix = isSelected ? "> " : "  ";

        return (
          <box key={r.id} height={1} width="100%">
            <text>
              {`${prefix}${badge}   ${shortId(r.id).padEnd(10)}  ${r.amount.padEnd(11)}  ${r.status.padEnd(10)}  ${purpose.padEnd(19)}  ${formatTimestamp(r.expires_at)}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
