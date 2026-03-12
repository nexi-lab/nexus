/**
 * Governance alert list with severity icons and selection for resolve action.
 */

import React from "react";
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

export function AlertList({ alerts, selectedIndex, loading }: AlertListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading alerts...</text>
      </box>
    );
  }

  if (alerts.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No governance alerts</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  SEV  CATEGORY         AGENT            MESSAGE                              STATUS     TIME"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ---  ---------------  ---------------  -------------------------------------  ---------  ----"}</text>
      </box>

      {/* Rows */}
      {alerts.map((alert, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const icon = SEVERITY_ICONS[alert.severity] ?? "?";
        const agent = alert.agent_id ?? "system";
        const message = alert.message.length > 37
          ? `${alert.message.slice(0, 34)}...`
          : alert.message;
        const status = alert.resolved ? "resolved" : "active";

        return (
          <box key={alert.alert_id} height={1} width="100%">
            <text>
              {`${prefix}${icon}  ${alert.category.padEnd(15)}  ${agent.padEnd(15)}  ${message.padEnd(37)}  ${status.padEnd(9)}  ${formatTimestamp(alert.created_at)}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
