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
 * - onSelect fires with the correct tab id on click
 */

import { describe, it, expect, mock, afterEach } from "bun:test";
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
  onSelect?: (id: string) => void,
): Promise<TestSetup> {
  setup = await testRender(
    <SubTabBar tabs={tabs} activeTab={activeTab} onSelect={onSelect} />,
    { width: 80, height: 3 },
  );
  await setup.renderOnce();
  return setup;
}

afterEach(() => {
  if (setup) {
    setup.renderer.destroy();
  }
});

// =============================================================================
// Render tests
// =============================================================================

describe("SubTabBar render", () => {
  it("renders active tab with brackets and inactive tabs with spaces", async () => {
    const { captureCharFrame } = await renderSubTabBar(
      [
        { id: "zones", label: "Zones" },
        { id: "bricks", label: "Bricks" },
        { id: "drift", label: "Drift" },
      ],
      "zones",
    );
    const frame = captureCharFrame();
    expect(frame).toContain("[Zones]");
    expect(frame).toContain(" Bricks ");
    expect(frame).toContain(" Drift ");
  });

  it("highlights the correct active tab when not first", async () => {
    const { captureCharFrame } = await renderSubTabBar(
      [
        { id: "alpha", label: "Alpha" },
        { id: "beta", label: "Beta" },
        { id: "gamma", label: "Gamma" },
      ],
      "beta",
    );
    const frame = captureCharFrame();
    expect(frame).toContain(" Alpha ");
    expect(frame).toContain("[Beta]");
    expect(frame).toContain(" Gamma ");
  });

  it("renders nothing for empty tabs", async () => {
    const { captureCharFrame } = await renderSubTabBar([], "any");
    expect(captureCharFrame().trim()).toBe("");
  });

  it("renders single tab with brackets", async () => {
    const { captureCharFrame } = await renderSubTabBar(
      [{ id: "only", label: "Only" }],
      "only",
    );
    expect(captureCharFrame()).toContain("[Only]");
  });

  it("renders all tabs as inactive when activeTab not in list", async () => {
    const { captureCharFrame } = await renderSubTabBar(
      [
        { id: "alpha", label: "Alpha" },
        { id: "beta", label: "Beta" },
      ],
      "missing",
    );
    const frame = captureCharFrame();
    expect(frame).toContain(" Alpha ");
    expect(frame).toContain(" Beta ");
    expect(frame).not.toContain("[");
  });
});

// =============================================================================
// Click (onSelect) tests
// =============================================================================

describe("SubTabBar click", () => {
  const TABS = [
    { id: "explorer", label: "Explorer" },
    { id: "links", label: "Links" },
    { id: "uploads", label: "Uploads" },
  ];

  it("fires onSelect with the first tab id when clicking at x=0", async () => {
    const onSelect = mock((_id: string) => {});
    const { mockMouse, renderOnce } = await renderSubTabBar(TABS, "explorer", onSelect);

    await mockMouse.click(0, 0);
    await renderOnce();

    const calls = onSelect.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    expect(calls[calls.length - 1]?.[0]).toBe("explorer");
  });

  it("fires onSelect with the second tab id when clicking into it", async () => {
    const onSelect = mock((_id: string) => {});
    const { mockMouse, renderOnce } = await renderSubTabBar(TABS, "explorer", onSelect);

    // "[Explorer] " = 11 chars; click in the middle of " Links "
    await mockMouse.click(14, 0);
    await renderOnce();

    const calls = onSelect.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    expect(calls[calls.length - 1]?.[0]).toBe("links");
  });

  it("does not throw when onSelect is not provided", async () => {
    const { mockMouse, renderOnce } = await renderSubTabBar(TABS, "explorer");

    // Should not throw — onSelect is optional
    await mockMouse.click(0, 0);
    await renderOnce();
  });
});
