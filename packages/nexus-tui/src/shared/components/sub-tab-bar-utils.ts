/**
 * Pure utility functions for sub-tab bar keyboard cycling.
 *
 * Separated from sub-tab-bar.tsx so tests can import without triggering
 * JSX compilation (matching tab-bar-utils.ts pattern).
 *
 * @see Issue #3498
 */

// =============================================================================
// Types
// =============================================================================

/** Minimal tab shape consumed by cycling helpers. */
export interface SubTab {
  readonly id: string;
  readonly label: string;
}

// =============================================================================
// Cycling helpers
// =============================================================================

/**
 * Cycle forward to the next tab (wraps around).
 *
 * If activeTab is not found in tabs (e.g. brick just disabled it),
 * defaults to the first tab to avoid undefined behavior.
 */
export function subTabForward<T extends string>(
  tabs: readonly { readonly id: T }[],
  activeTab: T,
  setActiveTab: (tab: T) => void,
): void {
  if (tabs.length === 0) return;
  const idx = tabs.findIndex((t) => t.id === activeTab);
  // Guard: if activeTab not in list, jump to first tab
  const nextIdx = idx === -1 ? 0 : (idx + 1) % tabs.length;
  const next = tabs[nextIdx];
  if (next) setActiveTab(next.id);
}

/**
 * Cycle backward to the previous tab (wraps around).
 *
 * Same guard as subTabForward for missing activeTab.
 */
export function subTabBackward<T extends string>(
  tabs: readonly { readonly id: T }[],
  activeTab: T,
  setActiveTab: (tab: T) => void,
): void {
  if (tabs.length === 0) return;
  const idx = tabs.findIndex((t) => t.id === activeTab);
  // Guard: if activeTab not in list, jump to first tab
  const prevIdx = idx === -1 ? 0 : (idx - 1 + tabs.length) % tabs.length;
  const prev = tabs[prevIdx];
  if (prev) setActiveTab(prev.id);
}

// =============================================================================
// Keyboard binding helper
// =============================================================================

/**
 * Returns a keybinding object with `tab` (forward) and `shift+tab` (backward)
 * entries that panels can spread into useKeyboard.
 *
 * Split-pane panels that need Shift+Tab for focus-toggle can override after
 * spreading: `{ ...subTabCycleBindings(...), "shift+tab": () => toggleFocus("zones") }`
 */
export function subTabCycleBindings<T extends string>(
  tabs: readonly { readonly id: T }[],
  activeTab: T,
  setActiveTab: (tab: T) => void,
): Record<string, () => void> {
  return {
    tab: () => subTabForward(tabs, activeTab, setActiveTab),
    "shift+tab": () => subTabBackward(tabs, activeTab, setActiveTab),
  };
}

// =============================================================================
// Tab fallback logic
// =============================================================================

/**
 * Determine if the active tab needs to fall back to the first visible tab.
 *
 * Returns the tab ID to switch to, or null if no switch is needed.
 * Pure function — the hook (useTabFallback) wraps this in a useEffect.
 */
export function tabFallback<T extends string>(
  visibleIds: readonly T[],
  activeTab: T,
): T | null {
  if (visibleIds.length === 0) return null;
  if (visibleIds.includes(activeTab)) return null;
  return visibleIds[0]!;
}
