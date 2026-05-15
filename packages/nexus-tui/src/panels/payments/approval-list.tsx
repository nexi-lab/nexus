/**
 * Approval list: displays spending approval requests with status coloring.
 */

import { For } from "solid-js";
import type { ApprovalRequest } from "../../stores/payments-store.js";
import { textStyle } from "../../shared/text-style.js";
import { statusColor } from "../../shared/theme.js";

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

export function ApprovalList(props: ApprovalListProps) {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading approvals..."
          : props.approvals.length === 0
            ? "No approval requests found"
            : `${props.approvals.length} approvals`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {/* Header */}
        <box height={1} width="100%">
          <text>{"  ID          AMOUNT       PURPOSE                    STATUS     REQUESTER     CREATED"}</text>
        </box>
        <box height={1} width="100%">
          <text>{"  ----------  -----------  -------------------------  ---------  ------------  -----------------------"}</text>
        </box>

        {/* Rows */}
        <For each={props.approvals}>{(a, i) => {
          const amount = String(a.amount).padEnd(11);
          const purpose = (a.purpose.length > 25 ? a.purpose.slice(0, 22) + "..." : a.purpose).padEnd(25);
          const color = STATUS_COLOR[a.status];

          return (
            <box height={1} width="100%">
              <text>
                {`${i() === props.selectedIndex ? "> " : "  "}${shortId(a.id).padEnd(10)}  ${amount}  ${purpose}  `}
                <span style={color ? textStyle({ fg: color }) : undefined}>{a.status.padEnd(9)}</span>
                {`  ${shortId(a.requester_id).padEnd(12)}  ${formatTime(a.created_at)}`}
              </text>
            </box>
          );
        }}</For>
      </scrollbox>
    </box>
  );
}
