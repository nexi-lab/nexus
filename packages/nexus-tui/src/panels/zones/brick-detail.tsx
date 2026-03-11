/**
 * Brick detail view: shows individual brick info from GET /api/v2/bricks/{name}.
 *
 * Displays: name, state, protocol_name, error, started_at, stopped_at, unmounted_at.
 */

import React from "react";
import type { BrickStatusResponse } from "../../stores/zones-store.js";

interface BrickDetailProps {
  readonly brick: BrickStatusResponse | null;
  readonly loading: boolean;
}

function formatEpoch(epoch: number | null): string {
  if (epoch === null) return "n/a";
  try {
    return new Date(epoch * 1000).toLocaleString();
  } catch {
    return String(epoch);
  }
}

export function BrickDetail({ brick, loading }: BrickDetailProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading brick detail...</text>
      </box>
    );
  }

  if (!brick) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Select a brick to view details</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      <box height={1} width="100%">
        <text>{`Name:         ${brick.name}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`State:        ${brick.state}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Protocol:     ${brick.protocol_name}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Error:        ${brick.error ?? "none"}`}</text>
      </box>

      <box height={1} width="100%" marginTop={1}>
        <text>--- Timestamps ---</text>
      </box>
      <box height={1} width="100%">
        <text>{`Started at:   ${formatEpoch(brick.started_at)}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Stopped at:   ${formatEpoch(brick.stopped_at)}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Unmounted at: ${formatEpoch(brick.unmounted_at)}`}</text>
      </box>
    </scrollbox>
  );
}
