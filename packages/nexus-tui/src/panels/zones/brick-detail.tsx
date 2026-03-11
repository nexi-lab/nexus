/**
 * Brick detail overview: status, type, address, capacity usage, last_seen.
 */

import React from "react";
import type { Brick } from "../../stores/zones-store.js";

interface BrickDetailProps {
  readonly brick: Brick | null;
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const exp = Math.floor(Math.log(bytes) / Math.log(1024));
  const idx = Math.min(exp, units.length - 1);
  const value = bytes / Math.pow(1024, idx);
  return `${value.toFixed(1)} ${units[idx]}`;
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return "n/a";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function renderUsageBar(used: number, total: number, width: number): string {
  if (total === 0) return `[${"?".repeat(width)}] n/a`;
  const pct = (used / total) * 100;
  const filled = Math.round((pct / 100) * width);
  const empty = width - filled;
  return `[${"#".repeat(filled)}${"-".repeat(empty)}] ${pct.toFixed(1)}%`;
}

const STATUS_LABELS: Readonly<Record<Brick["status"], string>> = {
  online: "Online",
  offline: "Offline",
  degraded: "Degraded",
  syncing: "Syncing",
};

export function BrickDetail({ brick }: BrickDetailProps): React.ReactNode {
  if (!brick) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Select a brick to view details</text>
      </box>
    );
  }

  const statusLabel = STATUS_LABELS[brick.status] ?? brick.status;

  return (
    <scrollbox height="100%" width="100%">
      <box height={1} width="100%">
        <text>{`Brick ID:  ${brick.brick_id}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Zone ID:   ${brick.zone_id}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Status:    ${statusLabel}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Type:      ${brick.brick_type}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Address:   ${brick.address}`}</text>
      </box>

      <box height={1} width="100%" marginTop={1}>
        <text>--- Capacity ---</text>
      </box>
      <box height={1} width="100%">
        <text>{`Used:      ${formatBytes(brick.used_bytes)} / ${formatBytes(brick.capacity_bytes)}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Usage:     ${renderUsageBar(brick.used_bytes, brick.capacity_bytes, 20)}`}</text>
      </box>

      <box height={1} width="100%" marginTop={1}>
        <text>{`Last seen: ${formatTimestamp(brick.last_seen)}`}</text>
      </box>
    </scrollbox>
  );
}
