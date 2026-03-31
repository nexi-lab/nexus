/**
 * Connector list view: shows registered connectors with status and capabilities.
 */

import React, { useCallback } from "react";
import type { Connector } from "../../stores/infra-store.js";
import { Spinner } from "../../shared/components/spinner.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { VirtualList } from "../../shared/components/virtual-list.js";

const VIEWPORT_HEIGHT = 20;

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
    return (
      <EmptyState
        message="No connectors registered."
        hint="Register a connector via the API: POST /api/v2/connectors"
      />
    );
  }

  const renderConnector = useCallback(
    (conn: Connector, i: number) => {
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
    },
    [selectedIndex],
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  St  Name                 Type          Capabilities"}</text>
      </box>

      <VirtualList
        items={connectors}
        renderItem={renderConnector}
        viewportHeight={VIEWPORT_HEIGHT}
        selectedIndex={selectedIndex}
      />
    </box>
  );
}
