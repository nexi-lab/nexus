/**
 * Pure utility functions for SideNav layout calculations.
 *
 * Separated from side-nav.tsx so tests can import without triggering
 * JSX compilation (matching tab-bar-utils.ts pattern).
 *
 * @see Issue #3497
 */

// =============================================================================
// Types
// =============================================================================

/** Responsive display mode for the sidebar. */
export type SideNavMode = "full" | "collapsed" | "hidden";

// =============================================================================
// Constants
// =============================================================================

/** Data not refreshed within this threshold (ms) is considered stale. */
export const STALE_THRESHOLD_MS = 60_000;

/** Minimum terminal width to show full labels. */
export const FULL_THRESHOLD = 120;

/** Minimum terminal width to show collapsed (icon + shortcut). */
export const COLLAPSED_THRESHOLD = 80;

/**
 * Character width of the sidebar in full mode.
 *
 * Layout: " S:Label____◂ " — 2 (left pad) + 1 (shortcut) + 1 (:) + label + 2 (indicator + right pad)
 * Longest full label is "Connectors" (10 chars) → 2 + 1 + 1 + 10 + 2 = 16.
 * Add 2 for breathing room = 18.
 */
export const SIDE_NAV_FULL_WIDTH = 18;

/**
 * Character width of the sidebar in collapsed mode.
 *
 * Box border consumes 2 chars (left + right), leaving 4 inner chars.
 * Layout: " ◎2◂" — 1 (pad) + 1 (icon) + 1 (shortcut) + 1 (indicator) = 4.
 */
export const SIDE_NAV_COLLAPSED_WIDTH = 6;

// =============================================================================
// Functions
// =============================================================================

/** Determine the sidebar display mode from terminal width. */
export function getSideNavMode(columns: number): SideNavMode {
  if (columns >= FULL_THRESHOLD) return "full";
  if (columns >= COLLAPSED_THRESHOLD) return "collapsed";
  return "hidden";
}

/** Get the sidebar pixel/character width for a given mode. */
export function getSideNavWidth(mode: SideNavMode): number {
  switch (mode) {
    case "full":
      return SIDE_NAV_FULL_WIDTH;
    case "collapsed":
      return SIDE_NAV_COLLAPSED_WIDTH;
    case "hidden":
      return 0;
  }
}
