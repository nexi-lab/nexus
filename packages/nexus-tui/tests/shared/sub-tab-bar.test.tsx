/**
 * Render tests for the SubTabBar component (#3498).
 *
 * Uses OpenTUI's testRender to verify actual terminal output.
 *
 * Covers:
 * - Renders visible tabs with active bracket highlight
 * - Empty tabs array renders nothing
 * - Single tab renders correctly
 * - Multiple tabs with spacing
 */

import { describe, it, expect, afterEach } from "bun:test";
import React from "react";
import { testRender } from "@opentui/react/test-utils";
import { SubTabBar } from "../../src/shared/components/sub-tab-bar.js";

// =============================================================================
// Helpers
// =============================================================================

type TestSetup = Awaited<ReturnType<typeof testRender>>;

let setup: TestSetup;

async function renderSubTabBar(
  tabs: Array<{ id: string; label: string }>,
  activeTab: string,
): Promise<string> {
  setup = await testRender(
    <SubTabBar tabs={tabs} activeTab={activeTab} />,
    { width: 80, height: 3 },
  );
  await setup.renderOnce();
  return setup.captureCharFrame();
}

afterEach(() => {
  if (setup) {
    setup.renderer.destroy();
  }
});

// =============================================================================
// Tests
// =============================================================================

describe("SubTabBar", () => {
  it("renders active tab with brackets and inactive tabs with spaces", async () => {
    const frame = await renderSubTabBar(
      [
        { id: "zones", label: "Zones" },
        { id: "bricks", label: "Bricks" },
        { id: "drift", label: "Drift" },
      ],
      "zones",
    );
    expect(frame).toContain("[Zones]");
    expect(frame).toContain(" Bricks ");
    expect(frame).toContain(" Drift ");
  });

  it("highlights the correct active tab when not first", async () => {
    const frame = await renderSubTabBar(
      [
        { id: "alpha", label: "Alpha" },
        { id: "beta", label: "Beta" },
        { id: "gamma", label: "Gamma" },
      ],
      "beta",
    );
    expect(frame).toContain(" Alpha ");
    expect(frame).toContain("[Beta]");
    expect(frame).toContain(" Gamma ");
  });

  it("renders nothing for empty tabs", async () => {
    const frame = await renderSubTabBar([], "any");
    // Should be blank — no tabs rendered
    expect(frame.trim()).toBe("");
  });

  it("renders single tab with brackets", async () => {
    const frame = await renderSubTabBar(
      [{ id: "only", label: "Only" }],
      "only",
    );
    expect(frame).toContain("[Only]");
  });

  it("renders all tabs as inactive when activeTab not in list", async () => {
    const frame = await renderSubTabBar(
      [
        { id: "alpha", label: "Alpha" },
        { id: "beta", label: "Beta" },
      ],
      "missing",
    );
    // Neither should have brackets
    expect(frame).toContain(" Alpha ");
    expect(frame).toContain(" Beta ");
    expect(frame).not.toContain("[");
  });
});
