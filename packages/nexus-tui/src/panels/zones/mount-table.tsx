/**
 * Mount table: shows mount points with path, type, and mounted_at timestamp.
 */

import React from "react";
import type { MountPoint } from "../../stores/zones-store.js";

interface MountTableProps {
  readonly mounts: readonly MountPoint[];
  readonly loading: boolean;
}

const TYPE_LABELS: Readonly<Record<MountPoint["mount_type"], string>> = {
  read: "R ",
  write: "W ",
  readwrite: "RW",
};

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function truncatePath(path: string, maxLen: number): string {
  if (path.length <= maxLen) return path;
  return `...${path.slice(-(maxLen - 3))}`;
}

export function MountTable({ mounts, loading }: MountTableProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading mounts...</text>
      </box>
    );
  }

  if (mounts.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No mount points found</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  PATH                           TYPE  MOUNTED AT"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  -----------------------------  ----  -------------------------"}</text>
      </box>

      {/* Rows */}
      {mounts.map((mount, i) => {
        const typeLabel = TYPE_LABELS[mount.mount_type] ?? mount.mount_type;
        const path = truncatePath(mount.path, 29);

        return (
          <box key={`mount-${i}`} height={1} width="100%">
            <text>
              {`  ${path.padEnd(29)}  ${typeLabel.padEnd(4)}  ${formatTimestamp(mount.mounted_at)}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
