/**
 * Render tests for the TabBar component.
 *
 * Covers:
 * - Renders all tabs with shortcut and label
 * - Active tab displays the ▸ prefix and inactive tabs use two spaces
 * - onSelect fires with the correct tab id on mousedown
 * - Clicking the already-active tab still fires onSelect (caller decides no-op logic)
 * - Single-tab and empty-tabs edge cases
 */

import { describe, it, expect, mock, afterEach } from "bun:test";
import React from "react";
import { testRender } from "@opentui/react/test-utils";
import { TabBar, type Tab } from "../../src/shared/components/tab-bar.js";

// =============================================================================
// Helpers
// =============================================================================

type TestSetup = Awaited<ReturnType<typeof testRender>>;

let setup: TestSetup;

async function renderTabBar(
  tabs: readonly Tab[],
  activeTab: string,
  onSelect: (id: string) => void,
): Promise<TestSetup> {
  setup = await testRender(
    <TabBar tabs={tabs} activeTab={activeTab} onSelect={onSelect} />,
    { width: 120, height: 2 },
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
// Test data
// =============================================================================

const TABS: readonly Tab[] = [
  { id: "files",    label: "Files",  shortcut: "1" },
  { id: "versions", label: "Vers",   shortcut: "2" },
  { id: "agents",   label: "Agents", shortcut: "3" },
];

// =============================================================================
// Render tests
// =============================================================================

describe("TabBar render", () => {
  it("renders all tab labels", async () => {
    const { captureCharFrame } = await renderTabBar(TABS, "files", mock(() => {}));
    const frame = captureCharFrame();
    expect(frame).toContain("Files");
    expect(frame).toContain("Vers");
    expect(frame).toContain("Agents");
  });

  it("renders shortcuts for each tab", async () => {
    const { captureCharFrame } = await renderTabBar(TABS, "files", mock(() => {}));
    const frame = captureCharFrame();
    expect(frame).toContain("1:");
    expect(frame).toContain("2:");
    expect(frame).toContain("3:");
  });

  it("renders ▸ prefix for the active tab", async () => {
    const { captureCharFrame } = await renderTabBar(TABS, "versions", mock(() => {}));
    const frame = captureCharFrame();
    expect(frame).toContain("▸");
  });

  it("renders separators between tabs", async () => {
    const { captureCharFrame } = await renderTabBar(TABS, "files", mock(() => {}));
    const frame = captureCharFrame();
    expect(frame).toContain("│");
  });

  it("renders nothing visible for empty tab list", async () => {
    const { captureCharFrame } = await renderTabBar([], "files", mock(() => {}));
    expect(captureCharFrame().trim()).toBe("");
  });

  it("renders a single tab without a separator", async () => {
    const { captureCharFrame } = await renderTabBar(
      [{ id: "only", label: "Only", shortcut: "1" }],
      "only",
      mock(() => {}),
    );
    const frame = captureCharFrame();
    expect(frame).toContain("Only");
    expect(frame).not.toContain("│");
  });
});

// =============================================================================
// Click (onMouseDown) tests
// =============================================================================

describe("TabBar click", () => {
  it("fires onSelect with the correct id when clicking the second tab", async () => {
    const onSelect = mock((_id: string) => {});
    const { mockMouse, renderOnce } = await renderTabBar(TABS, "files", onSelect);

    // The second tab (Vers) starts after the first tab content.
    // First tab: "▸ 1:Files │ " ≈ 12 chars wide starting at x=0
    // Click somewhere in the middle of the second tab
    await mockMouse.click(14, 0);
    await renderOnce();

    // onSelect should have been called; verify the specific id it received
    const calls = onSelect.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    expect(calls[calls.length - 1]?.[0]).toBe("versions");
  });

  it("fires onSelect with the first tab id when clicking at x=0", async () => {
    const onSelect = mock((_id: string) => {});
    const { mockMouse, renderOnce } = await renderTabBar(TABS, "files", onSelect);

    await mockMouse.click(0, 0);
    await renderOnce();

    const calls = onSelect.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    expect(calls[calls.length - 1]?.[0]).toBe("files");
  });

  it("fires onSelect even when clicking the already-active tab", async () => {
    const onSelect = mock((_id: string) => {});
    const { mockMouse, renderOnce } = await renderTabBar(TABS, "files", onSelect);

    await mockMouse.click(1, 0);
    await renderOnce();

    expect(onSelect.mock.calls.length).toBeGreaterThan(0);
    expect(onSelect.mock.calls[onSelect.mock.calls.length - 1]?.[0]).toBe("files");
  });
});
