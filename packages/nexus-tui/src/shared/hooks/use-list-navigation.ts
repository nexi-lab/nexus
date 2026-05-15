/**
 * Shared list navigation logic.
 *
 * Pure helper functions for cursor-in-list navigation used across all panels.
 * These are extracted from the repeated j/k/gg/G pattern to ensure consistency.
 *
 * For React integration, panels use these helpers within their useKeyboard bindings.
 *
 * @see Issue #3066 Architecture Decision 6A
 */

// =============================================================================
// Pure navigation helpers
// =============================================================================

/**
 * Clamp an index to valid range [0, length - 1].
 * Returns 0 for empty lists.
 */
export function clampIndex(index: number, length: number): number {
  if (length <= 0) return 0;
  return Math.max(0, Math.min(length - 1, index));
}

/**
 * Move index by delta, clamping to valid range.
 *
 * @param current - Current selected index
 * @param delta - Movement (+1 = down, -1 = up, +10 = page down, etc.)
 * @param length - Total number of items
 */
export function moveIndex(current: number, delta: number, length: number): number {
  return clampIndex(current + delta, length);
}

/**
 * Jump to the first item (gg in vim).
 */
export function jumpToStart(): number {
  return 0;
}

/**
 * Jump to the last item (G in vim).
 */
export function jumpToEnd(length: number): number {
  if (length <= 0) return 0;
  return length - 1;
}

// =============================================================================
// Keybinding builder
// =============================================================================

export interface ListNavigationOptions {
  /** Get the current selected index. */
  readonly getIndex: () => number;
  /** Set the new selected index. */
  readonly setIndex: (index: number) => void;
  /** Get the current list length. */
  readonly getLength: () => number;
  /** Called when Enter is pressed on the selected item. */
  readonly onSelect?: (index: number) => void;
}

/**
 * Build keyboard bindings for standard list navigation.
 *
 * Returns a Record<string, () => void> suitable for useKeyboard().
 * Panels merge these with their own panel-specific bindings.
 *
 * Includes: j/k (move), up/down (move), gg/G (jump), Enter (select)
 */
export function listNavigationBindings(
  options: ListNavigationOptions,
): Record<string, () => void> {
  const { getIndex, setIndex, getLength, onSelect } = options;

  const move = (delta: number) => () => {
    setIndex(moveIndex(getIndex(), delta, getLength()));
  };

  const bindings: Record<string, () => void> = {
    j: move(1),
    k: move(-1),
    down: move(1),
    up: move(-1),
  };

  // g = jump to start, G (shift+g) = jump to end
  bindings["g"] = () => setIndex(jumpToStart());
  bindings["shift+g"] = () => setIndex(jumpToEnd(getLength()));

  if (onSelect) {
    bindings["return"] = () => onSelect(getIndex());
  }

  return bindings;
}

/**
 * Render prefix for a list item: `> ` for selected, `  ` for others.
 */
export function selectionPrefix(index: number, selectedIndex: number): string {
  return index === selectedIndex ? "> " : "  ";
}
