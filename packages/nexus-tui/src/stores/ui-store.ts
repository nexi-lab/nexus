/**
 * Cross-cutting UI state store.
 *
 * Owns state that spans multiple panels: focus pane, zoom, scroll positions.
 * Keeps domain stores (files-store, agents-store, etc.) free of UI chrome.
 *
 * @see Issue #3066 Architecture Decision 2A
 */

import { create } from "zustand";
import type { PanelId } from "./global-store.js";

// =============================================================================
// Types
// =============================================================================

export type FocusPane = "left" | "right";

export interface UiState {
  /** Per-panel focus pane for split views. */
  readonly focusPane: Readonly<Record<string, FocusPane>>;

  /** Panel currently in fullscreen zoom mode, or null. */
  readonly zoomedPanel: PanelId | null;

  /** Persisted scroll positions keyed by "panel:list" identifier. */
  readonly scrollPositions: Readonly<Record<string, number>>;

  /** True when a global overlay (welcome, help, identity switcher) is active. */
  readonly overlayActive: boolean;

  /** True when a full-screen panel-level editor is open (suppresses global keybindings). */
  readonly fileEditorOpen: boolean;

  /** Whether the side navigation bar is visible (toggled via Ctrl+B). */
  readonly sideNavVisible: boolean;

  /** Timestamp (ms) of the last successful data update per panel. 0 = never fetched. */
  readonly panelDataTimestamps: Readonly<Partial<Record<PanelId, number>>>;

  /** Timestamp (ms) of the last time the user visited each panel. 0 = never visited. */
  readonly panelVisitTimestamps: Readonly<Partial<Record<PanelId, number>>>;

  /** Mirror of global-store activePanel, kept in sync by markPanelVisited. */
  readonly activePanelId: PanelId;

  // Actions
  readonly setFocusPane: (panel: string, pane: FocusPane) => void;
  readonly toggleFocusPane: (panel: string) => void;
  readonly getFocusPane: (panel: string) => FocusPane;
  readonly toggleZoom: (panel: PanelId) => void;
  readonly clearZoom: () => void;
  readonly setScrollPosition: (key: string, position: number) => void;
  readonly getScrollPosition: (key: string) => number;
  readonly setOverlayActive: (active: boolean) => void;
  readonly setFileEditorOpen: (open: boolean) => void;
  readonly toggleSideNav: () => void;
  readonly setSideNavVisible: (visible: boolean) => void;
  readonly markDataUpdated: (panel: PanelId) => void;
  readonly markPanelVisited: (panel: PanelId) => void;
  readonly resetFreshnessTimestamps: () => void;
}

// =============================================================================
// Store
// =============================================================================

export const useUiStore = create<UiState>((set, get) => ({
  focusPane: {},
  zoomedPanel: null,
  scrollPositions: {},
  overlayActive: false,
  fileEditorOpen: false,
  sideNavVisible: true,
  panelDataTimestamps: {},
  // "files" is the default active panel — mark it visited at startup so data
  // fetched during the initial load does not produce a false-positive unseen dot.
  panelVisitTimestamps: { files: Date.now() },
  activePanelId: "files" as PanelId,

  setFocusPane: (panel, pane) => {
    set((state) => ({
      focusPane: { ...state.focusPane, [panel]: pane },
    }));
  },

  toggleFocusPane: (panel) => {
    const current = get().focusPane[panel];
    const next: FocusPane = current === "left" ? "right" : current === "right" ? "left" : "right";
    set((state) => ({
      focusPane: { ...state.focusPane, [panel]: next },
    }));
  },

  getFocusPane: (panel) => {
    return get().focusPane[panel] ?? "left";
  },

  toggleZoom: (panel) => {
    set((state) => ({
      zoomedPanel: state.zoomedPanel === panel ? null : panel,
    }));
  },

  clearZoom: () => {
    set({ zoomedPanel: null });
  },

  setScrollPosition: (key, position) => {
    set((state) => ({
      scrollPositions: { ...state.scrollPositions, [key]: position },
    }));
  },

  getScrollPosition: (key) => {
    return get().scrollPositions[key] ?? 0;
  },

  setOverlayActive: (active) => {
    set({ overlayActive: active });
  },

  setFileEditorOpen: (open) => {
    set({ fileEditorOpen: open });
  },

  toggleSideNav: () => {
    set((state) => ({ sideNavVisible: !state.sideNavVisible }));
  },

  setSideNavVisible: (visible) => {
    set({ sideNavVisible: visible });
  },

  markDataUpdated: (panel) => {
    const now = Date.now();
    // If the user is currently viewing this panel, also update the visit
    // timestamp so the panel is not marked "unseen" when the user leaves.
    const isActive = get().activePanelId === panel;
    set((state) => ({
      panelDataTimestamps: { ...state.panelDataTimestamps, [panel]: now },
      ...(isActive
        ? { panelVisitTimestamps: { ...state.panelVisitTimestamps, [panel]: now } }
        : {}),
    }));
  },

  markPanelVisited: (panel) => {
    set((state) => ({
      panelVisitTimestamps: { ...state.panelVisitTimestamps, [panel]: Date.now() },
      activePanelId: panel,
    }));
  },

  resetFreshnessTimestamps: () => {
    set({
      panelDataTimestamps: {},
      panelVisitTimestamps: { [get().activePanelId]: Date.now() },
    });
  },
}));
