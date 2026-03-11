/**
 * Brick list sidebar: state indicator + name + protocol for each brick.
 */

import React from "react";
import type { BrickStatusResponse } from "../../stores/zones-store.js";

interface BrickListProps {
  readonly bricks: readonly BrickStatusResponse[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function stateIndicator(state: string): string {
  switch (state) {
    case "running":
      return "[ON]";
    case "stopped":
      return "[--]";
    case "failed":
      return "[!!]";
    case "mounted":
      return "[MT]";
    case "unmounted":
      return "[UM]";
    default:
      return "[??]";
  }
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
        const indicator = stateIndicator(brick.state);

        return (
          <box key={brick.name} height={1} width="100%">
            <text>{`${prefix}${indicator} ${brick.name} (${brick.protocol_name})`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
