/**
 * Windowed list rendering for terminal UIs.
 *
 * Only renders the visible rows plus an overscan buffer, instead of
 * materializing all items. This keeps the terminal responsive even for
 * 10k+ item lists.
 *
 * @see Issue #3102, Decision 1A
 */

import React, { useMemo } from "react";

export interface VirtualListProps<T> {
  /** Full (flat) list of items. */
  readonly items: readonly T[];
  /** Render callback for a single item row. */
  readonly renderItem: (item: T, index: number) => React.ReactNode;
  /** Height of each item in terminal rows. Default: 1 */
  readonly itemHeight?: number;
  /** Maximum number of visible rows in the viewport. */
  readonly viewportHeight: number;
  /** Currently selected/focused index (drives scroll position). */
  readonly selectedIndex: number;
  /** Extra rows rendered above and below the viewport. Default: 5 */
  readonly overscan?: number;
  /** Called with the absolute index when a row is clicked. */
  readonly onSelect?: (index: number) => void;
}

/**
 * Calculate the visible window for the given parameters.
 *
 * Exported for unit testing the pure math separately from React rendering.
 */
export function calculateWindow(
  totalItems: number,
  viewportHeight: number,
  selectedIndex: number,
  overscan: number,
): { startIndex: number; endIndex: number; scrollOffset: number } {
  if (totalItems === 0) {
    return { startIndex: 0, endIndex: 0, scrollOffset: 0 };
  }

  // Determine the scroll offset so the selected item is visible.
  // We keep a "follow" strategy: if selectedIndex would be outside the
  // current viewport, we shift the scroll offset to bring it into view.
  let scrollOffset = 0;

  if (totalItems <= viewportHeight) {
    // Everything fits — no scrolling needed
    scrollOffset = 0;
  } else {
    // Center the selected item in the viewport, clamped to valid range
    scrollOffset = Math.max(0, selectedIndex - Math.floor(viewportHeight / 2));
    const maxOffset = totalItems - viewportHeight;
    scrollOffset = Math.min(scrollOffset, maxOffset);
  }

  // Apply overscan to render a buffer above/below
  const startIndex = Math.max(0, scrollOffset - overscan);
  const endIndex = Math.min(totalItems, scrollOffset + viewportHeight + overscan);

  return { startIndex, endIndex, scrollOffset };
}

export function VirtualList<T>({
  items,
  renderItem,
  itemHeight = 1,
  viewportHeight,
  selectedIndex,
  overscan = 5,
  onSelect,
}: VirtualListProps<T>): React.ReactNode {
  const { startIndex, endIndex } = useMemo(
    () => calculateWindow(items.length, viewportHeight, selectedIndex, overscan),
    [items.length, viewportHeight, selectedIndex, overscan],
  );

  // Slice the items to only the visible window
  const visibleItems = useMemo(
    () => items.slice(startIndex, endIndex),
    [items, startIndex, endIndex],
  );

  if (items.length === 0) {
    return null;
  }

  return (
    <box flexDirection="column" height={viewportHeight * itemHeight} width="100%">
      {visibleItems.map((item, i) => {
        const absoluteIndex = startIndex + i;
        return onSelect ? (
          <box key={absoluteIndex} height={itemHeight} width="100%" onMouseDown={() => onSelect(absoluteIndex)}>
            {renderItem(item, absoluteIndex)}
          </box>
        ) : renderItem(item, absoluteIndex);
      })}
    </box>
  );
}
