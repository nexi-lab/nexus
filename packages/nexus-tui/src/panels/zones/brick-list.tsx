import type { JSX } from "solid-js";
/**
 * Brick list sidebar: state indicator + name + protocol for each brick.
 */

import type { BrickStatusResponse } from "../../stores/zones-store.js";
import { stateIndicator, stateColor } from "../../shared/brick-states.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { textStyle } from "../../shared/text-style.js";

interface BrickListProps {
  readonly bricks: readonly BrickStatusResponse[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

export function BrickList(props: BrickListProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>{"--- Bricks ---"}</text>
      <text>{`Total: ${props.bricks.length}${props.loading ? " (loading...)" : ""}`}</text>
      <scrollbox flexGrow={1} width="100%">
        {props.bricks.map((brick, i) => {
          const isSelected = i === props.selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const indicator = stateIndicator(brick.state);

        return (
          <box height={1} width="100%">
            <text>{prefix}</text>
            <text style={textStyle({ fg: stateColor(brick.state) })}>{indicator}</text>
            <text>{` ${brick.name} (${brick.protocol_name})`}</text>
          </box>
        );
      })}
    </scrollbox>
    </box>
  );
}
