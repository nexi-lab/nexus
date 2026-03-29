/**
 * Approval list: displays spending approval requests with status coloring.
 */

import React from "react";
import type { ApprovalRequest } from "../../stores/payments-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { statusColor } from "../../shared/theme.js";
import { EmptyState } from "../../shared/components/empty-state.js";

interface ApprovalListProps {
  readonly approvals: readonly ApprovalRequest[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function shortId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 8)}..`;
}

function formatTime(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

const STATUS_COLOR: Record<string, string> = {
  pending: statusColor.warning,
  approved: statusColor.healthy,
  rejected: statusColor.error,
};

export function ApprovalList({
  approvals,
  selectedIndex,
  loading,
}: ApprovalListProps): React.ReactNode {
  if (loading) {
    return <LoadingIndicator message="Loading approvals..." />;
  }

  if (approvals.length === 0) {
    return <EmptyState message="No approvals yet." hint="Press n to request approval." />;
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  ID          AMOUNT       PURPOSE                    STATUS     REQUESTER     CREATED"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ----------  -----------  -------------------------  ---------  ------------  -----------------------"}</text>
      </box>

      {/* Rows */}
      {approvals.map((a, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const amount = String(a.amount).padEnd(11);
        const purpose = (a.purpose.length > 25 ? a.purpose.slice(0, 22) + "..." : a.purpose).padEnd(25);
        const color = STATUS_COLOR[a.status];

        return (
          <box key={a.id} height={1} width="100%">
            <text>
              {`${prefix}${shortId(a.id).padEnd(10)}  ${amount}  ${purpose}  `}
              <span foregroundColor={color}>{a.status.padEnd(9)}</span>
              {`  ${shortId(a.requester_id).padEnd(12)}  ${formatTime(a.created_at)}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
