/**
 * Health view: brick health status with pass/fail/warn check indicators.
 */

import React from "react";
import type { BrickHealth, HealthCheck } from "../../stores/zones-store.js";

interface HealthViewProps {
  readonly health: BrickHealth | null;
  readonly loading: boolean;
}

const CHECK_BADGES: Readonly<Record<HealthCheck["status"], string>> = {
  pass: "[OK]",
  fail: "[!!]",
  warn: "[??]",
};

const STATUS_LABELS: Readonly<Record<BrickHealth["status"], string>> = {
  healthy: "Healthy",
  degraded: "Degraded",
  unhealthy: "Unhealthy",
  unknown: "Unknown",
};

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function HealthView({ health, loading }: HealthViewProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading health data...</text>
      </box>
    );
  }

  if (!health) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Select a brick to view health</text>
      </box>
    );
  }

  const statusLabel = STATUS_LABELS[health.status] ?? health.status;

  return (
    <scrollbox height="100%" width="100%">
      <box height={1} width="100%">
        <text>{`Status:      ${statusLabel}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Latency:     ${health.latency_ms} ms`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Error rate:  ${(health.error_rate * 100).toFixed(2)}%`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Last check:  ${formatTimestamp(health.last_check)}`}</text>
      </box>

      {health.checks.length > 0 && (
        <>
          <box height={1} width="100%" marginTop={1}>
            <text>--- Health Checks ---</text>
          </box>
          {health.checks.map((check, i) => {
            const badge = CHECK_BADGES[check.status] ?? "[??]";
            return (
              <box key={`check-${i}`} height={1} width="100%">
                <text>{`  ${badge} ${check.name}: ${check.message}`}</text>
              </box>
            );
          })}
        </>
      )}
    </scrollbox>
  );
}
