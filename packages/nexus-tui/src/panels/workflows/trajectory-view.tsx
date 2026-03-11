/**
 * Trajectory view: trajectory list and step detail.
 */

import React from "react";
import type { Trajectory } from "../../stores/workflows-store.js";

interface TrajectoryViewProps {
  readonly trajectories: readonly Trajectory[];
  readonly selectedIndex: number;
  readonly selectedTrajectory: Trajectory | null;
  readonly loading: boolean;
  readonly detailLoading: boolean;
}

const STATUS_BADGES: Readonly<Record<Trajectory["status"], string>> = {
  active: "[ACT]",
  completed: "[OK ]",
  failed: "[ERR]",
};

const STEP_BADGES: Readonly<Record<string, string>> = {
  completed: "[OK]",
  failed: "[ER]",
  skipped: "[SK]",
};

function formatTimestamp(ts: string | null): string {
  if (!ts) return "---";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function shortId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 8)}..`;
}

export function TrajectoryView({
  trajectories,
  selectedIndex,
  selectedTrajectory,
  loading,
  detailLoading,
}: TrajectoryViewProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading trajectories...</text>
      </box>
    );
  }

  if (trajectories.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No trajectories found</text>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Trajectory list (top half) */}
      <box height="50%" width="100%" flexDirection="column">
        <box height={1} width="100%">
          <text>{"  ST     TRAJECTORY    AGENT         STEPS  STARTED"}</text>
        </box>
        <box height={1} width="100%">
          <text>{"  -----  ----------  ------------  -----  -------"}</text>
        </box>
        <scrollbox flexGrow={1} width="100%">
          {trajectories.map((t, i) => {
            const isSelected = i === selectedIndex;
            const badge = STATUS_BADGES[t.status] ?? `[${t.status.toUpperCase()}]`;
            const prefix = isSelected ? "> " : "  ";

            return (
              <box key={t.trajectory_id} height={1} width="100%">
                <text>
                  {`${prefix}${badge.padEnd(5)}  ${shortId(t.trajectory_id).padEnd(10)}  ${shortId(t.agent_id).padEnd(12)}  ${String(t.step_count).padEnd(5)}  ${formatTimestamp(t.started_at)}`}
                </text>
              </box>
            );
          })}
        </scrollbox>
      </box>

      {/* Step detail (bottom half) */}
      <box height="50%" width="100%" borderStyle="single" flexDirection="column">
        <box height={1} width="100%">
          <text>--- Steps ---</text>
        </box>

        {detailLoading ? (
          <box flexGrow={1} justifyContent="center" alignItems="center">
            <text>Loading steps...</text>
          </box>
        ) : !selectedTrajectory ? (
          <box flexGrow={1} justifyContent="center" alignItems="center">
            <text>Select a trajectory to view steps</text>
          </box>
        ) : selectedTrajectory.steps.length === 0 ? (
          <box flexGrow={1} justifyContent="center" alignItems="center">
            <text>No steps recorded</text>
          </box>
        ) : (
          <scrollbox flexGrow={1} width="100%">
            {selectedTrajectory.steps.map((step, i) => {
              const badge = STEP_BADGES[step.status] ?? "[??]";
              const action = step.action.length > 24
                ? `${step.action.slice(0, 21)}...`
                : step.action;
              const output = step.output
                ? step.output.length > 30 ? `${step.output.slice(0, 27)}...` : step.output
                : "";

              return (
                <box key={step.step_id} height={1} width="100%">
                  <text>
                    {`  ${i + 1}. ${badge} ${action.padEnd(24)}  ${formatDuration(step.duration_ms).padEnd(8)}  ${output}`}
                  </text>
                </box>
              );
            })}
          </scrollbox>
        )}
      </box>
    </box>
  );
}
