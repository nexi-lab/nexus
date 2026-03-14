/**
 * Brick detail view: shows individual brick info from GET /api/v2/bricks/{name}.
 *
 * Displays: name, state, protocol, error, dependency graph, config (spec),
 * real FSM transition history, and available actions.
 */

import React from "react";
import type { BrickDetailResponse } from "../../stores/zones-store.js";
import { stateIndicator, allowedActionsForState } from "../../shared/brick-states.js";

interface BrickDetailProps {
  readonly brick: BrickDetailResponse | null;
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

const ACTION_KEYS: Readonly<Record<string, string>> = {
  mount: "M (shift)",
  unmount: "U",
  unregister: "D",
  remount: "m",
  reset: "x",
};

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

  const allowed = allowedActionsForState(brick.state);
  const actionHints = Array.from(allowed)
    .map((action) => {
      const key = ACTION_KEYS[action] ?? action;
      return `${key}:${action}`;
    })
    .join("  ");

  return (
    <scrollbox height="100%" width="100%">
      {/* Identity */}
      <box height={1} width="100%">
        <text>{`Name:         ${brick.name}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`State:        ${stateIndicator(brick.state)} ${brick.state}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Protocol:     ${brick.protocol_name}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Error:        ${brick.error ?? "none"}`}</text>
      </box>

      {/* Config (spec data) */}
      <box height={1} width="100%" marginTop={1}>
        <text>--- Configuration ---</text>
      </box>
      <box height={1} width="100%">
        <text>{`Enabled:      ${brick.enabled ? "yes" : "no"}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Retry count:  ${brick.retry_count}`}</text>
      </box>

      {/* Dependency graph */}
      <box height={1} width="100%" marginTop={1}>
        <text>--- Dependencies ---</text>
      </box>
      <box height={1} width="100%">
        <text>{`Depends on:   ${brick.depends_on.length > 0 ? brick.depends_on.join(", ") : "(none)"}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Depended by:  ${brick.depended_by.length > 0 ? brick.depended_by.join(", ") : "(none)"}`}</text>
      </box>

      {/* Timestamps */}
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

      {/* State history (real FSM transitions) */}
      <box height={1} width="100%" marginTop={1}>
        <text>--- State History ---</text>
      </box>
      {brick.transitions.length === 0 ? (
        <box height={1} width="100%">
          <text>  No transitions recorded</text>
        </box>
      ) : (
        brick.transitions.map((t, i) => (
          <box key={i} height={1} width="100%">
            <text>{`  ${t.from_state} → ${t.to_state}  (${t.event})`}</text>
          </box>
        ))
      )}

      {/* Available actions */}
      <box height={1} width="100%" marginTop={1}>
        <text>--- Available Actions ---</text>
      </box>
      <box height={1} width="100%">
        <text>{actionHints || "(none — brick is in a transient state)"}</text>
      </box>
    </scrollbox>
  );
}
