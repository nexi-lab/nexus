/**
 * Workspaces sub-tab for the Zones panel.
 * Displays registered workspace directories with selection highlighting.
 */

import React from "react";
import type { WorkspaceInfo } from "../../stores/workspace-store.js";

interface WorkspacesTabProps {
  readonly workspaces: readonly WorkspaceInfo[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

export function WorkspacesTab({
  workspaces,
  selectedIndex,
  loading,
}: WorkspacesTabProps): React.ReactNode {
  if (loading) return <text>Loading workspaces...</text>;
  if (workspaces.length === 0)
    return <text>No workspaces registered. Press 'n' to register one.</text>;

  return (
    <scrollbox height="100%" width="100%">
      {workspaces.map((ws, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        return (
          <box key={ws.path} height={1} width="100%">
            <text>{`${prefix}${ws.name}  ${ws.path}  ${ws.scope}  ${ws.created_by ?? ""}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
