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

export function McpMountsTab({
  mounts,
  selectedIndex,
  loading,
}: McpMountsTabProps): JSX.Element {
  if (loading) return <text>Loading MCP mounts...</text>;
  if (mounts.length === 0)
    return <text>No MCP servers mounted. Press 'n' to mount one.</text>;

  return (
    <scrollbox height="100%" width="100%">
      {mounts.map((mount, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const statusIcon = mount.mounted ? "\u25CF" : "\u25CB";
        return (
          <box height={1} width="100%">
            <text>{`${prefix}${statusIcon} ${mount.name}  ${mount.transport}  ${mount.tool_count} tools  ${mount.last_sync ?? "never synced"}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
