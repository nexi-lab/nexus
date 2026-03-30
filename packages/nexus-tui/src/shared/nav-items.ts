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
}

// =============================================================================
// Nav items
// =============================================================================

export const NAV_ITEMS: readonly NavItem[] = [
  { id: "files",          label: "Files", fullLabel: "Files",      shortcut: "1", icon: "□" },
  { id: "versions",       label: "Ver",   fullLabel: "Versions",   shortcut: "2", icon: "◎" },
  { id: "agents",         label: "Agent", fullLabel: "Agents",     shortcut: "3", icon: "◇" },
  { id: "zones",          label: "Zone",  fullLabel: "Zones",      shortcut: "4", icon: "⬡" },
  { id: "access",         label: "ACL",   fullLabel: "Access",     shortcut: "5", icon: "⊕" },
  { id: "payments",       label: "Pay",   fullLabel: "Payments",   shortcut: "6", icon: "◈" },
  { id: "search",         label: "Find",  fullLabel: "Search",     shortcut: "7", icon: "⊘" },
  { id: "workflows",      label: "Flow",  fullLabel: "Workflows",  shortcut: "8", icon: "⟲" },
  { id: "infrastructure", label: "Event", fullLabel: "Events",     shortcut: "9", icon: "◉" },
  { id: "console",        label: "CLI",   fullLabel: "Console",    shortcut: "0", icon: "▶" },
  { id: "connectors",     label: "Conn",  fullLabel: "Connectors", shortcut: "C", icon: "⊞" },
  { id: "stack",          label: "Stack", fullLabel: "Stack",      shortcut: "S", icon: "▦" },
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
