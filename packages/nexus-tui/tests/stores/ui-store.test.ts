/**
 * Tests for ui-store — cross-cutting UI state (focus, zoom, scroll).
 *
 * Written test-first (Decision 10A).
 */

import { describe, it, expect, beforeEach } from "bun:test";
import { useUiStore, type FocusPane } from "../../src/stores/ui-store.js";
import type { PanelId } from "../../src/stores/global-store.js";

describe("UiStore", () => {
  beforeEach(() => {
    useUiStore.setState({
      focusPane: {},
      zoomedPanel: null,
      scrollPositions: {},
    });
  });

  // ===========================================================================
  // Focus pane
  // ===========================================================================

  describe("setFocusPane", () => {
    it("sets focus pane for a panel", () => {
      useUiStore.getState().setFocusPane("files", "right");
      expect(useUiStore.getState().focusPane["files"]).toBe("right");
    });

    it("preserves other panels' focus", () => {
      useUiStore.getState().setFocusPane("files", "left");
      useUiStore.getState().setFocusPane("agents", "right");
      expect(useUiStore.getState().focusPane["files"]).toBe("left");
      expect(useUiStore.getState().focusPane["agents"]).toBe("right");
    });
  });

  describe("toggleFocusPane", () => {
    it("toggles from left to right", () => {
      useUiStore.getState().setFocusPane("files", "left");
      useUiStore.getState().toggleFocusPane("files");
      expect(useUiStore.getState().focusPane["files"]).toBe("right");
    });

    it("toggles from right to left", () => {
      useUiStore.getState().setFocusPane("files", "right");
      useUiStore.getState().toggleFocusPane("files");
      expect(useUiStore.getState().focusPane["files"]).toBe("left");
    });

    it("defaults to right when panel has no focus state", () => {
      useUiStore.getState().toggleFocusPane("agents");
      expect(useUiStore.getState().focusPane["agents"]).toBe("right");
    });
  });

  describe("getFocusPane", () => {
    it("returns the set focus pane", () => {
      useUiStore.getState().setFocusPane("files", "right");
      expect(useUiStore.getState().getFocusPane("files")).toBe("right");
    });

    it("returns 'left' as default for unset panels", () => {
      expect(useUiStore.getState().getFocusPane("files")).toBe("left");
    });
  });

  // ===========================================================================
  // Zoom
  // ===========================================================================

  describe("toggleZoom", () => {
    it("zooms in when not zoomed", () => {
      useUiStore.getState().toggleZoom("files");
      expect(useUiStore.getState().zoomedPanel).toBe("files");
    });

    it("unzooms when same panel is toggled", () => {
      useUiStore.getState().toggleZoom("files");
      useUiStore.getState().toggleZoom("files");
      expect(useUiStore.getState().zoomedPanel).toBeNull();
    });

    it("switches zoom to different panel", () => {
      useUiStore.getState().toggleZoom("files");
      useUiStore.getState().toggleZoom("agents");
      expect(useUiStore.getState().zoomedPanel).toBe("agents");
    });
  });

  describe("clearZoom", () => {
    it("clears zoom state", () => {
      useUiStore.getState().toggleZoom("files");
      useUiStore.getState().clearZoom();
      expect(useUiStore.getState().zoomedPanel).toBeNull();
    });

    it("no-ops when not zoomed", () => {
      useUiStore.getState().clearZoom();
      expect(useUiStore.getState().zoomedPanel).toBeNull();
    });
  });

  // ===========================================================================
  // Scroll positions
  // ===========================================================================

  describe("setScrollPosition", () => {
    it("stores scroll position by key", () => {
      useUiStore.getState().setScrollPosition("files:tree", 42);
      expect(useUiStore.getState().scrollPositions["files:tree"]).toBe(42);
    });

    it("preserves other scroll positions", () => {
      useUiStore.getState().setScrollPosition("files:tree", 10);
      useUiStore.getState().setScrollPosition("agents:list", 20);
      expect(useUiStore.getState().scrollPositions["files:tree"]).toBe(10);
      expect(useUiStore.getState().scrollPositions["agents:list"]).toBe(20);
    });

    it("overwrites existing scroll position", () => {
      useUiStore.getState().setScrollPosition("files:tree", 10);
      useUiStore.getState().setScrollPosition("files:tree", 99);
      expect(useUiStore.getState().scrollPositions["files:tree"]).toBe(99);
    });
  });

  describe("getScrollPosition", () => {
    it("returns stored position", () => {
      useUiStore.getState().setScrollPosition("files:tree", 42);
      expect(useUiStore.getState().getScrollPosition("files:tree")).toBe(42);
    });

    it("returns 0 for unknown key", () => {
      expect(useUiStore.getState().getScrollPosition("unknown")).toBe(0);
    });
  });
});
