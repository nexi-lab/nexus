/**
 * Brick list sidebar: state indicator + name + protocol for each brick.
 */

import React from "react";
import type { BrickStatusResponse } from "../../stores/zones-store.js";
import { stateIndicator, stateColor } from "../../shared/brick-states.js";
import { EmptyState } from "../../shared/components/empty-state.js";

interface BrickListProps {
  readonly bricks: readonly BrickStatusResponse[];
  readonly selectedIndex: number;
  readonly loading: boolean;
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
      <EmptyState
        message="No bricks registered."
        hint="Run 'nexus brick mount <name>' to add one."
      />
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
            <text>{prefix}</text>
            <text foregroundColor={stateColor(brick.state)}>{indicator}</text>
            <text>{` ${brick.name} (${brick.protocol_name})`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
