import type { JSX } from "solid-js";
/**
 * Brick list sidebar: state indicator + name + protocol for each brick.
 */

import { useZonesStore } from "../../stores/zones-store.js";
import { stateIndicator, stateColor } from "../../shared/brick-states.js";
import { textStyle } from "../../shared/text-style.js";

export function BrickList(): JSX.Element {
  // Read directly from store — reactive via Solid proxy with jsx:"preserve"
  const bricks = () => useZonesStore((s) => s.bricks);
  const selectedIndex = () => useZonesStore((s) => s.selectedIndex);
  const isLoading = () => useZonesStore((s) => s.isLoading);

  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>{`--- Bricks (${bricks().length}) ---${isLoading() ? " loading..." : ""}`}</text>
      <scrollbox flexGrow={1} width="100%">
        {bricks().map((brick, i) => {
          const isSelected = i === selectedIndex();
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
