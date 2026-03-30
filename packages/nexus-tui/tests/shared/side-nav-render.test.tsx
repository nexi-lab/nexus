/**
 * Render tests for the SideNav component (#3497).
 *
 * Uses OpenTUI's testRender to verify actual terminal output.
 *
 * Covers:
 * - Renders all 12 items with correct labels and shortcuts
 * - Highlights active panel with ◂ indicator
 * - Shows error dot (●) for panels with errors
 * - Responsive: full labels at wide, collapsed at medium, hidden at narrow
 * - Hidden when visible=false
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import React from "react";
import { testRender } from "@opentui/react/test-utils";
import { SideNav } from "../../src/shared/components/side-nav.js";
import { NAV_ITEMS } from "../../src/shared/nav-items.js";
import { useVersionsStore } from "../../src/stores/versions-store.js";
import { useZonesStore } from "../../src/stores/zones-store.js";
import { useFilesStore } from "../../src/stores/files-store.js";
import { useAgentsStore } from "../../src/stores/agents-store.js";

// =============================================================================
// Helpers
// =============================================================================

type TestSetup = Awaited<ReturnType<typeof testRender>>;

let setup: TestSetup;

async function renderSideNav(
  props: { activePanel?: string; visible?: boolean },
  options?: { width?: number; height?: number },
) {
  setup = await testRender(
    <SideNav
      activePanel={(props.activePanel ?? "files") as any}
      visible={props.visible ?? true}
    />,
    { width: options?.width ?? 140, height: options?.height ?? 20 },
  );
  await setup.renderOnce();
  return setup.captureCharFrame();
}

function resetStores(): void {
  // Reset stores that SideNav reads from
  useVersionsStore.setState({ isLoading: false, error: null });
  useZonesStore.setState({ isLoading: false, error: null });
  useFilesStore.setState({ error: null });
  useAgentsStore.setState({ error: null });
}

// =============================================================================
// Tests
// =============================================================================

describe("SideNav render", () => {
  beforeEach(() => {
    resetStores();
  });

  afterEach(() => {
    if (setup) {
      setup.renderer.destroy();
    }
  });

  // ===========================================================================
  // Item rendering
  // ===========================================================================

  describe("renders all 12 items", () => {
    it("shows all full labels at wide terminal", async () => {
      const frame = await renderSideNav({ activePanel: "files" }, { width: 140 });

      for (const item of NAV_ITEMS) {
        expect(frame).toContain(item.fullLabel);
      }
    });

    it("shows all shortcuts", async () => {
      const frame = await renderSideNav({ activePanel: "files" }, { width: 140 });

      for (const item of NAV_ITEMS) {
        // Each shortcut appears as "S:" in full mode
        expect(frame).toContain(`${item.shortcut}:`);
      }
    });
  });

  // ===========================================================================
  // Active panel indicator
  // ===========================================================================

  describe("active panel indicator", () => {
    it("shows ◂ indicator for active panel", async () => {
      const frame = await renderSideNav({ activePanel: "files" }, { width: 140 });
      // The active panel line should contain the ◂ character
      expect(frame).toContain("◂");
    });

    it("shows ◂ on the correct panel when switching", async () => {
      const frame = await renderSideNav({ activePanel: "payments" }, { width: 140 });

      // Split into lines and find the one with ◂
      const lines = frame.split("\n");
      const activeLine = lines.find((l) => l.includes("◂"));
      expect(activeLine).toBeDefined();
      expect(activeLine!).toContain("Payments");
    });

    it("only shows one ◂ indicator", async () => {
      const frame = await renderSideNav({ activePanel: "zones" }, { width: 140 });
      const count = (frame.match(/◂/g) || []).length;
      expect(count).toBe(1);
    });
  });

  // ===========================================================================
  // Loading indicator
  // ===========================================================================

  describe("loading indicator", () => {
    it("shows spinner frame for panel with loading state", async () => {
      useVersionsStore.setState({ isLoading: true });
      const frame = await renderSideNav({ activePanel: "files" }, { width: 140 });

      // The versions line should have a braille spinner character instead of a space
      const lines = frame.split("\n");
      const versionsLine = lines.find((l) => l.includes("Versions"));
      expect(versionsLine).toBeDefined();
      // Spinner frames are braille chars: ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏
      // The line should NOT end with a plain space before the border
      // It should contain one of the spinner frames
      const hasSpinner = /[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]/.test(versionsLine!);
      expect(hasSpinner).toBe(true);
    });

    it("does not show spinner when not loading", async () => {
      const frame = await renderSideNav({ activePanel: "files" }, { width: 140 });
      const hasSpinner = /[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]/.test(frame);
      expect(hasSpinner).toBe(false);
    });
  });

  // ===========================================================================
  // Error indicator
  // ===========================================================================

  describe("error indicator", () => {
    it("shows ● for panel with error state", async () => {
      useFilesStore.setState({ error: "test error" });
      const frame = await renderSideNav({ activePanel: "versions" }, { width: 140 });

      // The files line should have the error dot
      const lines = frame.split("\n");
      const filesLine = lines.find((l) => l.includes("Files"));
      expect(filesLine).toBeDefined();
      expect(filesLine!).toContain("●");
    });

    it("does not show ● when no errors", async () => {
      const frame = await renderSideNav({ activePanel: "files" }, { width: 140 });

      // Count ● symbols — should be zero (no errors set)
      const errorDots = (frame.match(/●/g) || []).length;
      expect(errorDots).toBe(0);
    });

    it("shows ● on multiple panels with errors simultaneously", async () => {
      useFilesStore.setState({ error: "err1" });
      useAgentsStore.setState({ error: "err2" });
      const frame = await renderSideNav({ activePanel: "versions" }, { width: 140 });

      const errorDots = (frame.match(/●/g) || []).length;
      expect(errorDots).toBe(2);
    });
  });

  // ===========================================================================
  // Responsive breakpoints
  // ===========================================================================

  describe("responsive breakpoints", () => {
    it("shows full labels at >= 120 columns", async () => {
      const frame = await renderSideNav({ activePanel: "files" }, { width: 120 });
      expect(frame).toContain("Connectors");
      expect(frame).toContain("Workflows");
    });

    it("shows icons at 80-119 columns (collapsed mode)", async () => {
      const frame = await renderSideNav({ activePanel: "files" }, { width: 100 });

      // In collapsed mode, should see icons and shortcuts but not full labels
      for (const item of NAV_ITEMS) {
        expect(frame).toContain(item.icon);
      }
      // Full labels should NOT appear
      expect(frame).not.toContain("Connectors");
      expect(frame).not.toContain("Workflows");
    });

    it("renders nothing at < 80 columns (hidden mode)", async () => {
      const frame = await renderSideNav({ activePanel: "files" }, { width: 70 });
      // Should not contain any nav item labels or icons
      expect(frame).not.toContain("Files");
      expect(frame).not.toContain("◂");
    });
  });

  // ===========================================================================
  // Visibility
  // ===========================================================================

  describe("visibility", () => {
    it("renders nothing when visible=false", async () => {
      const frame = await renderSideNav({ visible: false }, { width: 140 });
      expect(frame).not.toContain("Files");
      expect(frame).not.toContain("◂");
    });
  });
});
