/**
 * Panel navigation items — single source of truth for SideNav rendering,
 * keyboard shortcuts, and panel-to-store indicator mapping.
 *
 * Extracted from app.tsx to keep layout concerns separate from data.
 *
 * @see Issue #3497
 */

import type { PanelId } from "../stores/global-store.js";

// =============================================================================
// Types
// =============================================================================

export interface NavItem {
  readonly id: PanelId;
  /** Short label for collapsed / narrow terminals */
  readonly label: string;
  /** Full label for wide terminals */
  readonly fullLabel: string;
  /** Keyboard shortcut displayed in the nav */
  readonly shortcut: string;
  /** Single-char icon for collapsed mode */
  readonly icon: string;
  /**
   * Brick(s) required for this panel tab to be visible in the navigation.
   * Matches TabDef.brick semantics: null = always visible, string = single brick,
   * string[] = any one of the listed bricks must be enabled.
   */
  readonly brick: string | readonly string[] | null;
}

// =============================================================================
// Nav items
// =============================================================================

export const NAV_ITEMS: readonly NavItem[] = [
  { id: "files",          label: "Files", fullLabel: "Files",      shortcut: "1", icon: "□", brick: null },
  { id: "versions",       label: "Ver",   fullLabel: "Versions",   shortcut: "2", icon: "◎", brick: "versioning" },
  { id: "agents",         label: "Agent", fullLabel: "Agents",     shortcut: "3", icon: "◇", brick: ["agent_runtime", "delegation", "ipc"] },
  { id: "zones",          label: "Zone",  fullLabel: "Zones",      shortcut: "4", icon: "⬡", brick: null },
  { id: "access",         label: "ACL",   fullLabel: "Access",     shortcut: "5", icon: "⊕", brick: ["access_manifest", "governance", "auth", "delegation"] },
  { id: "payments",       label: "Pay",   fullLabel: "Payments",   shortcut: "6", icon: "◈", brick: "pay" },
  { id: "search",         label: "Find",  fullLabel: "Search",     shortcut: "7", icon: "⊘", brick: null },
  { id: "workflows",      label: "Flow",  fullLabel: "Workflows",  shortcut: "8", icon: "⟲", brick: "workflows" },
  { id: "infrastructure", label: "Event", fullLabel: "Events",     shortcut: "9", icon: "◉", brick: null },
  { id: "console",        label: "CLI",   fullLabel: "Console",    shortcut: "0", icon: "▶", brick: null },
  // Connectors and Stack have no global keyboard shortcut — any uppercase letter
  // can collide with panel-local bindings in OpenTUI's broadcast model. These
  // panels are reachable via the command palette (":") only.
  // "·" (U+00B7) and "○" (U+25CB) signal palette-only access; neither looks
  // like a pressable key so users won't be misled into trying to use them.
  { id: "connectors",     label: "Conn",  fullLabel: "Connectors", shortcut: "·", icon: "⊞", brick: "storage" },
  { id: "stack",          label: "Stack", fullLabel: "Stack",      shortcut: "○", icon: "▦", brick: null },
];

// =============================================================================
// Per-panel indicator selectors (Decision 1A: thin adapter map)
//
// Only stores that expose loading/error state are wired up.
// Panels without indicators simply return false.
// =============================================================================

/**
 * Zustand-compatible selector: reads loading state from a store.
 * Returns a boolean for use with Object.is equality (Decision 4A).
 */
export type IndicatorSelector<S> = (state: S) => boolean;

/**
 * Mapping from PanelId to the store hook + selector needed to read
 * loading and error state. Stored as getState() thunks so they can
 * be called outside React (in tests) or inside hooks.
 *
 * Lazy-imported in side-nav.tsx to avoid circular deps.
 */
export interface PanelIndicators {
  readonly loading: boolean;
  readonly error: boolean;
}

/** Default indicators for panels whose stores don't expose loading/error. */
export const NO_INDICATORS: PanelIndicators = { loading: false, error: false };

/**
 * Runtime array of all PanelId values derived from NAV_ITEMS.
 * Use in tests and completeness checks to avoid hardcoding the list.
 * Lives here (not in global-store.ts) so it can be imported without
 * pulling in the zustand runtime dependency.
 */
export const ALL_PANEL_IDS: readonly PanelId[] = NAV_ITEMS.map((item) => item.id);
