/**
 * Scheduler metrics dashboard: queued, running, completed, failed, throughput.
 */

import React from "react";
import type { SchedulerMetrics } from "../../stores/workflows-store.js";

interface SchedulerViewProps {
  readonly metrics: SchedulerMetrics | null;
  readonly loading: boolean;
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function SchedulerView({ metrics, loading }: SchedulerViewProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading scheduler metrics...</text>
      </box>
    );
  }

  if (!metrics) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No scheduler metrics available</text>
      </box>
    );
  }

  const total = metrics.queued_tasks + metrics.running_tasks + metrics.completed_tasks + metrics.failed_tasks;

  return (
    <scrollbox height="100%" width="100%">
      {/* Task counts */}
      <box height={1} width="100%">
        <text>--- Task Counts ---</text>
      </box>
      <box height={1} width="100%">
        <text>{`  Queued:     ${metrics.queued_tasks}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`  Running:    ${metrics.running_tasks}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`  Completed:  ${metrics.completed_tasks}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`  Failed:     ${metrics.failed_tasks}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`  Total:      ${total}`}</text>
      </box>

      {/* Performance */}
      <box height={1} width="100%" marginTop={1}>
        <text>--- Performance ---</text>
      </box>
      <box height={1} width="100%">
        <text>{`  Avg wait time:     ${formatMs(metrics.avg_wait_ms)}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`  Avg duration:      ${formatMs(metrics.avg_duration_ms)}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`  Throughput:        ${metrics.throughput_per_minute.toFixed(1)}/min`}</text>
      </box>

      {/* Success rate */}
      {total > 0 && (
        <>
          <box height={1} width="100%" marginTop={1}>
            <text>--- Success Rate ---</text>
          </box>
          <box height={1} width="100%">
            <text>{`  ${((metrics.completed_tasks / total) * 100).toFixed(1)}% completed  |  ${((metrics.failed_tasks / total) * 100).toFixed(1)}% failed`}</text>
          </box>
        </>
      )}
    </scrollbox>
  );
}
