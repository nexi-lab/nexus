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
      sideNavVisible: true,
      panelDataTimestamps: {},
      panelVisitTimestamps: { files: Date.now() },
      activePanelId: "files" as any,
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
  // Side nav visibility
  // ===========================================================================

  describe("toggleSideNav", () => {
    it("hides sidebar when visible", () => {
      expect(useUiStore.getState().sideNavVisible).toBe(true);
      useUiStore.getState().toggleSideNav();
      expect(useUiStore.getState().sideNavVisible).toBe(false);
    });

    it("shows sidebar when hidden", () => {
      useUiStore.getState().setSideNavVisible(false);
      useUiStore.getState().toggleSideNav();
      expect(useUiStore.getState().sideNavVisible).toBe(true);
    });

    it("round-trips correctly", () => {
      useUiStore.getState().toggleSideNav();
      useUiStore.getState().toggleSideNav();
      expect(useUiStore.getState().sideNavVisible).toBe(true);
    });
  });

  describe("setSideNavVisible", () => {
    it("sets to false", () => {
      useUiStore.getState().setSideNavVisible(false);
      expect(useUiStore.getState().sideNavVisible).toBe(false);
    });

    it("sets to true", () => {
      useUiStore.getState().setSideNavVisible(false);
      useUiStore.getState().setSideNavVisible(true);
      expect(useUiStore.getState().sideNavVisible).toBe(true);
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

  // ===========================================================================
  // Panel data timestamps (#3503)
  // ===========================================================================

  describe("markDataUpdated", () => {
    it("records a timestamp for the panel", () => {
      const before = Date.now();
      useUiStore.getState().markDataUpdated("files");
      const after = Date.now();

      const ts = useUiStore.getState().panelDataTimestamps["files"];
      expect(ts).toBeDefined();
      expect(ts!).toBeGreaterThanOrEqual(before);
      expect(ts!).toBeLessThanOrEqual(after);
    });

    it("preserves other panels' timestamps", () => {
      useUiStore.getState().markDataUpdated("files");
      useUiStore.getState().markDataUpdated("agents");

      expect(useUiStore.getState().panelDataTimestamps["files"]).toBeDefined();
      expect(useUiStore.getState().panelDataTimestamps["agents"]).toBeDefined();
    });

    it("overwrites previous timestamp for the same panel", () => {
      useUiStore.getState().markDataUpdated("files");
      const first = useUiStore.getState().panelDataTimestamps["files"]!;

      useUiStore.getState().markDataUpdated("files");
      const second = useUiStore.getState().panelDataTimestamps["files"]!;

      expect(second).toBeGreaterThanOrEqual(first);
    });

    it("also updates visit timestamp when panel is currently active", () => {
      // "files" is the default active panel in global-store
      useUiStore.getState().markDataUpdated("files");

      const dataTs = useUiStore.getState().panelDataTimestamps["files"]!;
      const visitTs = useUiStore.getState().panelVisitTimestamps["files"]!;

      // Visit should be updated to match data, preventing false unseen
      expect(visitTs).toBeGreaterThanOrEqual(dataTs);
    });

    it("does not update visit timestamp for non-active panel", () => {
      // Reset visit timestamp for agents (not the active panel)
      useUiStore.setState({
        panelVisitTimestamps: { ...useUiStore.getState().panelVisitTimestamps, agents: 100 },
      });

      useUiStore.getState().markDataUpdated("agents");

      // Visit timestamp should remain at 100 (not updated)
      expect(useUiStore.getState().panelVisitTimestamps["agents"]).toBe(100);
    });
  });

  // ===========================================================================
  // Panel visit timestamps (#3503)
  // ===========================================================================

  describe("markPanelVisited", () => {
    it("records a timestamp for the panel", () => {
      const before = Date.now();
      useUiStore.getState().markPanelVisited("versions");
      const after = Date.now();

      const ts = useUiStore.getState().panelVisitTimestamps["versions"];
      expect(ts).toBeDefined();
      expect(ts!).toBeGreaterThanOrEqual(before);
      expect(ts!).toBeLessThanOrEqual(after);
    });

    it("preserves other panels' visit timestamps", () => {
      useUiStore.getState().markPanelVisited("files");
      useUiStore.getState().markPanelVisited("agents");

      expect(useUiStore.getState().panelVisitTimestamps["files"]).toBeDefined();
      expect(useUiStore.getState().panelVisitTimestamps["agents"]).toBeDefined();
    });
  });

  // ===========================================================================
  // Reset freshness timestamps (#3503)
  // ===========================================================================

  describe("resetFreshnessTimestamps", () => {
    it("clears data timestamps and preserves only active panel visit", () => {
      useUiStore.getState().markDataUpdated("files");
      useUiStore.getState().markDataUpdated("agents");
      useUiStore.getState().markPanelVisited("agents");

      useUiStore.getState().resetFreshnessTimestamps();

      expect(useUiStore.getState().panelDataTimestamps).toEqual({});
      // Active panel (agents) should still be marked visited
      expect(useUiStore.getState().panelVisitTimestamps["agents"]).toBeDefined();
      // Other panels should be cleared
      expect(useUiStore.getState().panelVisitTimestamps["files"]).toBeUndefined();
    });
  });
});
