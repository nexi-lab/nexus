import type { JSX } from "solid-js";
/**
 * MCP Mounts sub-tab for the Zones panel.
 * Displays mounted MCP servers with selection highlighting and status icons.
 */

import type { McpMount } from "../../stores/mcp-store.js";

interface McpMountsTabProps {
  readonly mounts: readonly McpMount[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

export function McpMountsTab(props: McpMountsTabProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading MCP mounts..."
          : props.mounts.length === 0
            ? "No MCP servers mounted. Press 'n' to mount one."
            : `${props.mounts.length} MCP mounts`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {props.mounts.map((mount, i) => {
          const isSelected = i === props.selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const statusIcon = mount.mounted ? "\u25CF" : "\u25CB";
          return (
            <box height={1} width="100%">
              <text>{`${prefix}${statusIcon} ${mount.name}  ${mount.transport}  ${mount.tool_count} tools  ${mount.last_sync ?? "never synced"}`}</text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
