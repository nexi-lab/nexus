/**
 * Shared hook for filtering panel sub-tabs based on enabled bricks.
 *
 * Provides the TabDef type and useVisibleTabs hook (Decision 2A: hybrid
 * shared type, per-panel ownership).
 */

import { useMemo } from "react";
import { useShallow } from "zustand/react/shallow";
import { useGlobalStore } from "../../stores/global-store.js";
import { filterVisibleTabs } from "../tab-visibility.js";

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
  const enabledBricks = useGlobalStore(useShallow((s) => s.enabledBricks));
  const featuresLoaded = useGlobalStore((s) => s.featuresLoaded);

  return useMemo(
    () => filterVisibleTabs(allTabs, enabledBricks, featuresLoaded),
    [allTabs, enabledBricks, featuresLoaded],
  );
}
