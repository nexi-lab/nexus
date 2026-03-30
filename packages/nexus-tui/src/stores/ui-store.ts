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
}));
