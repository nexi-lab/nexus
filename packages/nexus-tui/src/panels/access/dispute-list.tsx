/**
 * Dispute list: displays filed disputes with status, tier, parties, and reason.
 */

import React from "react";
import type { Dispute } from "../../stores/access-store.js";

interface DisputeListProps {
  readonly disputes: readonly Dispute[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function shortId(id: string): string {
  if (id.length <= 10) return id;
  return `${id.slice(0, 8)}..`;
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function DisputeList({
  disputes,
  selectedIndex,
  loading,
}: DisputeListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading disputes...</text>
      </box>
    );
  }

  if (disputes.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No disputes found. Use 'f' to file a dispute by exchange ID.</text>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={1} width="100%">
        <text>{`Disputes: ${disputes.length}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ID          STATUS      TIER  FILED               COMPLAINANT   RESPONDENT    REASON"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ----------  ----------  ----  ------------------  ----------    ----------    -------------------------"}</text>
      </box>

      {/* Rows */}
      <scrollbox flexGrow={1} width="100%">
        {disputes.map((d, i) => {
          const isSelected = i === selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const id = shortId(d.id).padEnd(10);
          const status = d.status.padEnd(10);
          const tier = String(d.tier).padEnd(4);
          const filed = formatTimestamp(d.filed_at).padEnd(18);
          const complainant = shortId(d.complainant_agent_id).padEnd(10);
          const respondent = shortId(d.respondent_agent_id).padEnd(10);
          const reason = d.reason.length > 25 ? `${d.reason.slice(0, 22)}...` : d.reason;

          return (
            <box key={d.id} height={1} width="100%">
              <text>
                {`${prefix}${id}  ${status}  ${tier}  ${filed}  ${complainant}    ${respondent}    ${reason}`}
              </text>
            </box>
          );
        })}
      </scrollbox>

      {/* Selected dispute detail */}
      {disputes[selectedIndex] && (
        <box height={3} width="100%" flexDirection="column">
          <text>{`Resolution: ${disputes[selectedIndex]!.resolution ?? "(pending)"}`}</text>
          <text>{`Escrow: ${disputes[selectedIndex]!.escrow_amount ?? "none"}  Released: ${disputes[selectedIndex]!.escrow_released}`}</text>
          <text>{`Appeal deadline: ${disputes[selectedIndex]!.appeal_deadline ? formatTimestamp(disputes[selectedIndex]!.appeal_deadline!) : "n/a"}`}</text>
        </box>
      )}
    </box>
  );
}
