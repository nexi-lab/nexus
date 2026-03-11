/**
 * Brick list sidebar: status indicator + brick_id + zone_id for each brick.
 */

import React from "react";
import type { Brick } from "../../stores/zones-store.js";

interface BrickListProps {
  readonly bricks: readonly Brick[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

const STATUS_INDICATORS: Readonly<Record<Brick["status"], string>> = {
  online: "[ON]",
  offline: "[--]",
  degraded: "[DG]",
  syncing: "[SY]",
};

function shortId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 10)}..`;
}

export function BrickList({
  bricks,
  selectedIndex,
  loading,
}: BrickListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading bricks...</text>
      </box>
    );
  }

  if (bricks.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No bricks found</text>
      </box>
    );
  }

  return (
    <scrollbox flexGrow={1} width="100%">
      {bricks.map((brick, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const indicator = STATUS_INDICATORS[brick.status] ?? "[??]";

        return (
          <box key={brick.brick_id} height={1} width="100%">
            <text>{`${prefix}${indicator} ${shortId(brick.brick_id)} (${brick.zone_id})`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
