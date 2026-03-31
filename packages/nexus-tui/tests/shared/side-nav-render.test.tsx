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
import { useFilesStore } from "../../src/stores/files-store.js";
import { useVersionsStore } from "../../src/stores/versions-store.js";
import { useAgentsStore } from "../../src/stores/agents-store.js";
import { useZonesStore } from "../../src/stores/zones-store.js";
import { useAccessStore } from "../../src/stores/access-store.js";
import { usePaymentsStore } from "../../src/stores/payments-store.js";
import { useSearchStore } from "../../src/stores/search-store.js";
import { useWorkflowsStore } from "../../src/stores/workflows-store.js";
import { useInfraStore } from "../../src/stores/infra-store.js";
import { useApiConsoleStore } from "../../src/stores/api-console-store.js";
import { useConnectorsStore } from "../../src/stores/connectors-store.js";
import { useStackStore } from "../../src/stores/stack-store.js";
import { useUiStore } from "../../src/stores/ui-store.js";

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
  // Reset ALL stores that SideNav reads loading/error from.
  // Must be exhaustive — other test suites may leave stale error state.
  useFilesStore.setState({ error: null });
  useVersionsStore.setState({ isLoading: false, error: null });
  useAgentsStore.setState({ error: null });
  useZonesStore.setState({ isLoading: false, error: null });
  useAccessStore.setState({ error: null });
  usePaymentsStore.setState({ error: null });
  useSearchStore.setState({ error: null });
  useWorkflowsStore.setState({ error: null });
  useInfraStore.setState({ error: null });
  useApiConsoleStore.setState({ isLoading: false });
  useConnectorsStore.setState({ error: null });
  useStackStore.setState({ error: null });
  // Reset unseen/stale timestamps (files visited at startup matches production init)
  useUiStore.setState({ panelDataTimestamps: {}, panelVisitTimestamps: { files: Date.now() } });
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
  // Unseen indicator (#3503)
  // ===========================================================================

  describe("unseen indicator", () => {
    it("shows blue ● for panel with unseen data (not active)", async () => {
      // Data updated at t=1000, never visited → unseen
      useUiStore.setState({
        panelDataTimestamps: { versions: 1000 },
        panelVisitTimestamps: {},
      });
      const frame = await renderSideNav({ activePanel: "files" }, { width: 140 });

      const lines = frame.split("\n");
      const versionsLine = lines.find((l) => l.includes("Versions"));
      expect(versionsLine).toBeDefined();
      expect(versionsLine!).toContain("●");
    });

    it("does not show unseen ● for the active panel", async () => {
      // Data updated but panel is active → show ◂ not ●
      useUiStore.setState({
        panelDataTimestamps: { files: 1000 },
        panelVisitTimestamps: {},
      });
      const frame = await renderSideNav({ activePanel: "files" }, { width: 140 });

      const lines = frame.split("\n");
      const filesLine = lines.find((l) => l.includes("Files"));
      expect(filesLine).toBeDefined();
      expect(filesLine!).toContain("◂");
    });

    it("clears unseen after visit timestamp exceeds data timestamp", async () => {
      // Data at t=1000, visited at t=2000 → no longer unseen
      useUiStore.setState({
        panelDataTimestamps: { versions: 1000 },
        panelVisitTimestamps: { versions: 2000 },
      });
      const frame = await renderSideNav({ activePanel: "files" }, { width: 140 });

      const lines = frame.split("\n");
      const versionsLine = lines.find((l) => l.includes("Versions"));
      expect(versionsLine).toBeDefined();
      // Should not have ● (no unseen, no error)
      expect(versionsLine!).not.toContain("●");
    });
  });

  // ===========================================================================
  // Stale indicator (#3503)
  // ===========================================================================

  describe("stale indicator", () => {
    it("does not show stale for panels with no data yet", async () => {
      // No data timestamps → not stale (never fetched)
      const frame = await renderSideNav({ activePanel: "files" }, { width: 140 });

      // All inactive panels should use normal muted text, not faint
      // Verify no error dots — panels are just "healthy"
      const errorDots = (frame.match(/●/g) || []).length;
      expect(errorDots).toBe(0);
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
