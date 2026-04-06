import type { JSX } from "solid-js";
/**
 * Trajectories tab: simple list view of agent trajectories.
 *
 * Displays trace_id, agent_id, status, started_at, and step_count.
 */

import type { TrajectoryItem } from "../../stores/agents-store.js";
import { StyledText } from "../../shared/components/styled-text.js";

interface TrajectoriesTabProps {
  readonly trajectories: readonly TrajectoryItem[];
  readonly loading: boolean;
}

export function TrajectoriesTab(props: TrajectoriesTabProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading trajectories..."
          : props.trajectories.length === 0
            ? "No trajectories found."
            : `${props.trajectories.length} trajectories`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {/* Header */}
        <box height={1} width="100%">
          <text>{"  TRACE_ID           AGENT_ID         STATUS      STARTED             STEPS"}</text>
        </box>

        {props.trajectories.map((traj) => {
          const traceShort = traj.trace_id.slice(0, 16) + "...";
          const agentShort = traj.agent_id.slice(0, 14);
          const started = traj.started_at ? traj.started_at.slice(0, 19) : "n/a";
          return (
            <box height={1} width="100%">
              <StyledText>{`  ${traceShort}  ${agentShort.padEnd(16)}  ${traj.status.padEnd(10)}  ${started}  ${traj.step_count}`}</StyledText>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
