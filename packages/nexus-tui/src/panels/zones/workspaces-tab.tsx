import type { JSX } from "solid-js";
/**
 * Workspaces sub-tab for the Zones panel.
 * Displays registered workspace directories with selection highlighting.
 */

import type { WorkspaceInfo } from "../../stores/workspace-store.js";

interface WorkspacesTabProps {
  readonly workspaces: readonly WorkspaceInfo[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

export function WorkspacesTab(props: WorkspacesTabProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading workspaces..."
          : props.workspaces.length === 0
            ? "No workspaces registered. Press 'n' to register one."
            : `${props.workspaces.length} workspaces`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {props.workspaces.map((ws, i) => {
          const isSelected = i === props.selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          return (
            <box height={1} width="100%">
              <text>{`${prefix}${ws.name}  ${ws.path}  ${ws.scope}  ${ws.created_by ?? ""}`}</text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
