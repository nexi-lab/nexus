/**
 * Connector capabilities detail view.
 *
 * Shown when Enter is pressed on a selected connector.
 * Fetches and displays capabilities from GET /api/v2/connectors/{name}/capabilities.
 */

import React from "react";
import { Spinner } from "../../shared/components/spinner.js";

export interface ConnectorDetailProps {
  readonly connectorName: string;
  readonly capabilities: unknown | null;
  readonly loading: boolean;
}

export function ConnectorDetail({
  connectorName,
  capabilities,
  loading,
}: ConnectorDetailProps): React.ReactNode {
  if (loading) {
    return <Spinner label={`Loading capabilities for ${connectorName}...`} />;
  }

  if (capabilities === null || capabilities === undefined) {
    return <text>{`No capabilities data for ${connectorName}`}</text>;
  }

  // Render capabilities as formatted JSON lines (truncated to prevent huge renders)
  const json = JSON.stringify(capabilities, null, 2);
  const display = json.length > 5000 ? json.slice(0, 5000) + "\n... (truncated)" : json;
  const lines = display.split("\n");

  return (
    <box flexDirection="column" height="100%" width="100%">
      <box height={1} width="100%">
        <text>{`Capabilities: ${connectorName}`}</text>
      </box>
      <scrollbox flexGrow={1} width="100%">
        {lines.map((line, i) => (
          <box key={i} height={1} width="100%">
            <text>{`  ${line}`}</text>
          </box>
        ))}
      </scrollbox>
    </box>
  );
}
