/**
 * Windowed list rendering for terminal UIs.
 *
 * Only renders the visible rows plus an overscan buffer, instead of
 * materializing all items. This keeps the terminal responsive even for
 * 10k+ item lists.
 *
 * @see Issue #3102, Decision 1A
 */

import { createMemo, Show } from "solid-js";
import type { JSX } from "solid-js";

export interface VirtualListProps<T> {
  /** Full (flat) list of items. */
  readonly items: readonly T[];
  /** Render callback for a single item row. */
  readonly renderItem: (item: T, index: number) => JSX.Element;
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

export function VirtualList<T>(props: VirtualListProps<T>): JSX.Element {
  // SolidJS: do NOT destructure props — use props.x for reactive access.
  const itemHeight = props.itemHeight ?? 1;
  const overscan = props.overscan ?? 5;

  const windowMemo = createMemo(
    () => calculateWindow(props.items.length, props.viewportHeight, props.selectedIndex, overscan),
  );
  const startIndex = () => windowMemo().startIndex;
  const endIndex = () => windowMemo().endIndex;

  // Slice the items to only the visible window
  const visibleItems = createMemo(
    () => props.items.slice(startIndex(), endIndex()),
  );

  return (
    <Show when={props.items.length > 0}>
      <box flexDirection="column" height={props.viewportHeight * itemHeight} width="100%">
        {visibleItems().map((item, i) => {
          const absoluteIndex = startIndex() + i;
          return props.onSelect ? (
            <box height={itemHeight} width="100%" onMouseDown={() => props.onSelect!(absoluteIndex)}>
              {props.renderItem(item, absoluteIndex)}
            </box>
          ) : props.renderItem(item, absoluteIndex);
        })}
      </box>
    </Show>
  );
}
