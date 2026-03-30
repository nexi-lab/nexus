/**
 * Hook to fall back to the first visible tab when the active tab
 * becomes hidden (e.g. its brick was disabled).
 *
 * Replaces the inline useEffect that was duplicated across 6+ panels
 * with inconsistent dependency arrays.
 *
 * @see Issue #3498
 */

import { useEffect } from "react";
import { tabFallback } from "../components/sub-tab-bar-utils.js";

/**
 * If activeTab is not in visibleTabs, switch to the first visible tab.
 *
 * Uses visibleIds.join(",") as the dependency key to match the established
 * codebase convention (see zones-panel, events-panel, etc.).
 */
export function useTabFallback<T extends string>(
  visibleTabs: readonly { readonly id: T }[],
  activeTab: T,
  setActiveTab: (tab: T) => void,
): void {
  const visibleIds = visibleTabs.map((t) => t.id);

  useEffect(() => {
    const target = tabFallback(visibleIds, activeTab);
    if (target !== null) setActiveTab(target);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleIds.join(","), activeTab]);
}
