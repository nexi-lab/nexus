import type { JSX } from "solid-js";
/**
 * Connector list view: shows registered connectors with status and capabilities.
 */

import type { Connector } from "../../stores/infra-store.js";
import { VirtualList } from "../../shared/components/virtual-list.js";

const VIEWPORT_HEIGHT = 20;

const STATUS_ICON: Record<string, string> = {
  active: "●",
  inactive: "○",
  error: "✗",
};

export function ConnectorList(props: {
  readonly connectors: readonly Connector[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}): JSX.Element {
  const renderConnector = (conn: Connector, i: number) => {
    const prefix = i === props.selectedIndex ? "> " : "  ";
    const icon = STATUS_ICON[conn.status] ?? "?";
    const name = conn.name.padEnd(20).slice(0, 20);
    const type = conn.type.padEnd(13).slice(0, 13);
    const caps = conn.capabilities.join(", ");

    return (
      <box height={1} width="100%">
        <text>{`${prefix}${icon}  ${name} ${type} ${caps}`}</text>
      </box>
    );
  };

  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading connectors..."
          : props.connectors.length === 0
            ? "No connectors registered. Register a connector via the API: POST /api/v2/connectors"
            : `${props.connectors.length} connectors`}
      </text>

      {/* Header */}
      <box height={1} width="100%">
        <text>{"  St  Name                 Type          Capabilities"}</text>
      </box>

      <VirtualList
        items={props.connectors}
        renderItem={renderConnector}
        viewportHeight={VIEWPORT_HEIGHT}
        selectedIndex={props.selectedIndex}
      />
    </box>
  );
}
