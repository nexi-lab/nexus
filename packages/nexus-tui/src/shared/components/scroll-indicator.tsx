/**
 * Scroll position indicator wrapper.
 * Shows ▲/▼ arrows when list is scrollable in either direction.
 * @see Issue #3066, Phase A4
 */

import React from "react";
import { statusColor } from "../theme.js";

interface ScrollIndicatorProps {
  /** Currently selected/focused index */
  readonly selectedIndex: number;
  /** Total number of items in the list */
  readonly totalItems: number;
  /** Number of visible items in the viewport (approximate) */
  readonly visibleItems: number;
  readonly children: React.ReactNode;
}

export function ScrollIndicator({
  selectedIndex,
  totalItems,
  visibleItems,
  children,
}: ScrollIndicatorProps): React.ReactNode {
  const showTop = selectedIndex > 0;
  const showBottom = selectedIndex < totalItems - 1 && totalItems > visibleItems;

  return (
    <box flexDirection="column" height="100%" width="100%">
      {showTop && (
        <box height={1} width="100%" justifyContent="center">
          <text foregroundColor={statusColor.dim}>{"▲ more above"}</text>
        </box>
      )}
      <box flexGrow={1}>{children}</box>
      {showBottom && (
        <box height={1} width="100%" justifyContent="center">
          <text foregroundColor={statusColor.dim}>{"▼ more below"}</text>
        </box>
      )}
    </box>
  );
}
