/**
 * Shared hook for filtering panel sub-tabs based on enabled bricks.
 *
 * Provides the TabDef type and useVisibleTabs hook (Decision 2A: hybrid
 * shared type, per-panel ownership).
 */

import { useMemo } from "react";
import { useGlobalStore } from "../../stores/global-store.js";

/**
 * Definition of a sub-tab within a panel.
 *
 * Each panel defines its own ALL_TABS: TabDef[] array. The useVisibleTabs
 * hook filters this array based on which bricks are currently enabled.
 */
export interface TabDef<T extends string = string> {
  /** Tab identifier (matches the panel's tab union type). */
  readonly id: T;
  /** Display label shown in the sub-tab bar. */
  readonly label: string;
  /**
   * Brick(s) required for this tab to be visible.
   * - string: single brick must be enabled
   * - string[]: any of the listed bricks must be enabled (OR semantics)
   * - null: tab is always visible (no brick dependency)
   */
  readonly brick: string | readonly string[] | null;
}

export function filterTabs<T extends string>(
  allTabs: readonly TabDef<T>[],
  enabledBricks: readonly string[],
  featuresLoaded: boolean,
): readonly TabDef<T>[] {
  if (!featuresLoaded) return allTabs;

  return allTabs.filter((tab) => {
    if (tab.brick === null) return true;
    if (typeof tab.brick === "string") return enabledBricks.includes(tab.brick);
    return tab.brick.some((b) => enabledBricks.includes(b));
  });
}

/**
 * Filter a panel's tab definitions to only those whose required bricks
 * are currently enabled.
 *
 * Uses shallow comparison on enabledBricks to avoid unnecessary re-renders
 * (Decision 15A).
 */
export function useVisibleTabs<T extends string>(
  allTabs: readonly TabDef<T>[],
): readonly TabDef<T>[] {
  const enabledBricks = useGlobalStore((s) => s.enabledBricks);
  const featuresLoaded = useGlobalStore((s) => s.featuresLoaded);

  return useMemo(() => {
    return filterTabs(allTabs, enabledBricks, featuresLoaded);
  }, [allTabs, enabledBricks, featuresLoaded]);
}
