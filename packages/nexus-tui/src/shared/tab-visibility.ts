import type { TabDef } from "./hooks/use-visible-tabs.js";

export function filterVisibleTabs<T extends string>(
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
