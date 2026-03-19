/**
 * Trajectories tab: simple list view of agent trajectories.
 *
 * Displays trace_id, agent_id, status, started_at, and step_count.
 */

import React from "react";
import type { TrajectoryItem } from "../../stores/agents-store.js";
import { StyledText } from "../../shared/components/styled-text.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";

interface TrajectoriesTabProps {
  readonly trajectories: readonly TrajectoryItem[];
  readonly loading: boolean;
}

export function TrajectoriesTab({ trajectories, loading }: TrajectoriesTabProps): React.ReactNode {
  if (loading) return <LoadingIndicator message="Loading trajectories..." />;
  if (trajectories.length === 0) return <text>No trajectories found.</text>;

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  TRACE_ID           AGENT_ID         STATUS      STARTED             STEPS"}</text>
      </box>

      {trajectories.map((traj) => {
        const traceShort = traj.trace_id.slice(0, 16) + "...";
        const agentShort = traj.agent_id.slice(0, 14);
        const started = traj.started_at ? traj.started_at.slice(0, 19) : "n/a";
        return (
          <box key={traj.trace_id} height={1} width="100%">
            <StyledText>{`  ${traceShort}  ${agentShort.padEnd(16)}  ${traj.status.padEnd(10)}  ${started}  ${traj.step_count}`}</StyledText>
          </box>
        );
      })}
    </scrollbox>
  );
}
