/**
 * Connector list view: shows registered connectors with status and capabilities.
 */

import React from "react";
import type { Connector } from "../../stores/infra-store.js";
import { Spinner } from "../../shared/components/spinner.js";

const STATUS_ICON: Record<string, string> = {
  active: "●",
  inactive: "○",
  error: "✗",
};

export function ConnectorList({
  connectors,
  selectedIndex,
  loading,
}: {
  readonly connectors: readonly Connector[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}): React.ReactNode {
  if (loading) {
    return <Spinner label="Loading connectors..." />;
  }

  if (connectors.length === 0) {
    return <text>No connectors registered</text>;
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  St  Name                 Type          Capabilities"}</text>
      </box>

      {connectors.map((conn, i) => {
        const prefix = i === selectedIndex ? "> " : "  ";
        const icon = STATUS_ICON[conn.status] ?? "?";
        const name = conn.name.padEnd(20).slice(0, 20);
        const type = conn.type.padEnd(13).slice(0, 13);
        const caps = conn.capabilities.join(", ");

        return (
          <box key={conn.connector_id} height={1} width="100%">
            <text>{`${prefix}${icon}  ${name} ${type} ${caps}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
