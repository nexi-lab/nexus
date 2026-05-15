import { Show } from "solid-js";
import type { JSX } from "solid-js";
/**
 * Scroll position indicator wrapper.
 * Shows ▲/▼ arrows when list is scrollable in either direction.
 * @see Issue #3066, Phase A4
 */


import { statusColor } from "../theme.js";
import { textStyle } from "../text-style.js";

interface ScrollIndicatorProps {
  /** Currently selected/focused index */
  readonly selectedIndex: number;
  /** Total number of items in the list */
  readonly totalItems: number;
  /** Number of visible items in the viewport (approximate) */
  readonly visibleItems: number;
  readonly children: JSX.Element;
}

export function ScrollIndicator(props: ScrollIndicatorProps): JSX.Element {
  const isScrollable = () => props.totalItems > props.visibleItems;
  const showTop = () => isScrollable() && props.selectedIndex > 0;
  const showBottom = () => isScrollable() && props.selectedIndex < props.totalItems - 1;

  return (
    <box flexDirection="column" height="100%" width="100%">
      <Show when={showTop()}>
        <box height={1} width="100%" justifyContent="center">
          <text style={textStyle({ fg: statusColor.dim })}>{"▲ more above"}</text>
        </box>
      </Show>
      <box flexGrow={1}>{props.children}</box>
      <Show when={showBottom()}>
        <box height={1} width="100%" justifyContent="center">
          <text style={textStyle({ fg: statusColor.dim })}>{"▼ more below"}</text>
        </box>
      </Show>
    </box>
  );
}
