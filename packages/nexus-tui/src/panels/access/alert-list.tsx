import type { JSX } from "solid-js";
/**
 * Governance alert list with severity icons and selection for resolve action.
 */

import type { GovernanceAlert } from "../../stores/access-store.js";

interface AlertListProps {
  readonly alerts: readonly GovernanceAlert[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

const SEVERITY_ICONS: Readonly<Record<GovernanceAlert["severity"], string>> = {
  critical: "●",
  warning: "◐",
  info: "○",
};

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function AlertList(props: AlertListProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading alerts..."
          : props.alerts.length === 0
            ? "No alerts found."
            : `${props.alerts.length} alerts`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {/* Header */}
        <box height={1} width="100%">
          <text>{"  SEV  TYPE             AGENT            DETAILS                              STATUS     TIME"}</text>
        </box>
        <box height={1} width="100%">
          <text>{"  ---  ---------------  ---------------  -------------------------------------  ---------  ----"}</text>
        </box>

        {/* Rows */}
        {props.alerts.map((alert, i) => {
          const isSelected = i === props.selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const icon = SEVERITY_ICONS[alert.severity] ?? "?";
          const agent = alert.agent_id ?? "system";
          const detailStr = typeof alert.details === "string"
            ? alert.details
            : JSON.stringify(alert.details ?? "");
          const details = detailStr.length > 37
            ? `${detailStr.slice(0, 34)}...`
            : detailStr;
          const status = alert.resolved ? "resolved" : "active";
          const time = alert.created_at ? formatTimestamp(alert.created_at) : "-";

          return (
            <box height={1} width="100%">
              <text>
                {`${prefix}${icon}  ${alert.alert_type.padEnd(15)}  ${agent.padEnd(15)}  ${details.padEnd(37)}  ${status.padEnd(9)}  ${time}`}
              </text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
