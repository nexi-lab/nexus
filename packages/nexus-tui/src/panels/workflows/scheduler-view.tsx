import type { JSX } from "solid-js";
/**
 * Scheduler metrics dashboard: queued, running, completed, failed, throughput.
 */

import type { SchedulerMetrics } from "../../stores/workflows-store.js";

interface SchedulerViewProps {
  readonly metrics: SchedulerMetrics | null;
  readonly loading: boolean;
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function SchedulerView(props: SchedulerViewProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading scheduler metrics..."
          : !props.metrics
            ? "No scheduler metrics available"
            : `--- Astraea Scheduler (${props.metrics.use_hrrn ? "HRRN" : "FIFO"}) ---`}
      </text>

      {(() => {
        if (props.loading || !props.metrics) return null;
        const metrics = props.metrics;
        const total = metrics.queued_tasks + metrics.running_tasks + metrics.completed_tasks + metrics.failed_tasks;

        return (
          <scrollbox flexGrow={1} width="100%">
            <box height={1} width="100%">
              <text>{""}</text>
            </box>

            {/* Task counts */}
            <box height={1} width="100%">
              <text>{"  Task Counts:"}</text>
            </box>
            <box height={1} width="100%">
              <text>{`    Queued:     ${metrics.queued_tasks}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{`    Running:    ${metrics.running_tasks}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{`    Completed:  ${metrics.completed_tasks}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{`    Failed:     ${metrics.failed_tasks}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{`    Total:      ${total}`}</text>
            </box>

            {/* Queue by priority class */}
            {metrics.queue_by_class && metrics.queue_by_class.length > 0 && (
              <>
                <box height={1} width="100%">
                  <text>{""}</text>
                </box>
                <box height={1} width="100%">
                  <text>{"  Queue by Priority:"}</text>
                </box>
                {metrics.queue_by_class.map((c, i) => (
                  <box height={1} width="100%">
                    <text>{`    ${(c.priority_class ?? "unknown").padEnd(12)} ${c.count} tasks`}</text>
                  </box>
                ))}
              </>
            )}

            {/* Fair share */}
            {metrics.fair_share && Object.keys(metrics.fair_share).length > 0 && (
              <>
                <box height={1} width="100%">
                  <text>{""}</text>
                </box>
                <box height={1} width="100%">
                  <text>{"  Fair Share Allocation:"}</text>
                </box>
                {Object.entries(metrics.fair_share).map(([agent, share], i) => (
                  <box height={1} width="100%">
                    <text>{`    ${agent.padEnd(20)} ${JSON.stringify(share)}`}</text>
                  </box>
                ))}
              </>
            )}

            {/* Performance */}
            {(metrics.avg_wait_ms > 0 || metrics.throughput_per_minute > 0) && (
              <>
                <box height={1} width="100%">
                  <text>{""}</text>
                </box>
                <box height={1} width="100%">
                  <text>{"  Performance:"}</text>
                </box>
                <box height={1} width="100%">
                  <text>{`    Avg wait:       ${formatMs(metrics.avg_wait_ms)}`}</text>
                </box>
                <box height={1} width="100%">
                  <text>{`    Avg duration:   ${formatMs(metrics.avg_duration_ms)}`}</text>
                </box>
                <box height={1} width="100%">
                  <text>{`    Throughput:     ${metrics.throughput_per_minute.toFixed(1)}/min`}</text>
                </box>
              </>
            )}

            {/* Success rate */}
            {total > 0 && (
              <>
                <box height={1} width="100%">
                  <text>{""}</text>
                </box>
                <box height={1} width="100%">
                  <text>{`  Success: ${((metrics.completed_tasks / total) * 100).toFixed(1)}%  |  Failed: ${((metrics.failed_tasks / total) * 100).toFixed(1)}%`}</text>
                </box>
              </>
            )}
          </scrollbox>
        );
      })()}
    </box>
  );
}
