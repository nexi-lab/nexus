/**
 * Tab type and pure utility functions for the tab bar (#3243).
 *
 * Separated from tab-bar.tsx so tests can import without triggering
 * JSX compilation (matching the codebase pattern where pure logic
 * is testable without React context).
 */

export interface Tab {
  readonly id: string;
  readonly label: string;
  readonly fullLabel?: string;
  readonly shortcut: string;
}

/**
 * Compute the rendered character width of the tab bar for a given label mode.
 *
 * Layout per tab: prefix (2) + shortcut (1) + ":" (1) + label length.
 * Separator between tabs: " │ " (3 chars).
 */
export function computeTabBarWidth(tabs: readonly Tab[], useFullLabels: boolean): number {
  let width = 0;
  for (let i = 0; i < tabs.length; i++) {
    const tab = tabs[i]!;
    const label = useFullLabels ? (tab.fullLabel ?? tab.label) : tab.label;
    width += 4 + label.length;
    if (i < tabs.length - 1) width += 3;
  }
  return width;
}

/** Returns true when full labels fit within the given column count. */
export function shouldUseFullLabels(tabs: readonly Tab[], columns: number): boolean {
  return computeTabBarWidth(tabs, true) <= columns;
}
