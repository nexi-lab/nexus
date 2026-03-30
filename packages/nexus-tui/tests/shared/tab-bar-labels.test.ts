/**
 * Tests for responsive tab bar label logic (#3243).
 *
 * Covers:
 * - computeTabBarWidth: correct character-width calculations
 * - shouldUseFullLabels: dynamic threshold based on total full-label width
 * - Edge cases: undefined fullLabel, single tab, empty tabs, boundary columns
 */

import { describe, it, expect } from "bun:test";
import { computeTabBarWidth, shouldUseFullLabels } from "../../src/shared/components/tab-bar-utils.js";
import type { Tab } from "../../src/shared/components/tab-bar-utils.js";

// =============================================================================
// Test data: mirrors the TABS array in app.tsx
// =============================================================================

const APP_TABS: readonly Tab[] = [
  { id: "files", label: "Files", fullLabel: "Files", shortcut: "1" },
  { id: "versions", label: "Ver", fullLabel: "Versions", shortcut: "2" },
  { id: "agents", label: "Agent", fullLabel: "Agents", shortcut: "3" },
  { id: "zones", label: "Zone", fullLabel: "Zones", shortcut: "4" },
  { id: "access", label: "ACL", fullLabel: "Access", shortcut: "5" },
  { id: "payments", label: "Pay", fullLabel: "Payments", shortcut: "6" },
  { id: "search", label: "Find", fullLabel: "Search", shortcut: "7" },
  { id: "workflows", label: "Flow", fullLabel: "Workflows", shortcut: "8" },
  { id: "infrastructure", label: "Event", fullLabel: "Events", shortcut: "9" },
  { id: "console", label: "CLI", fullLabel: "Console", shortcut: "0" },
  { id: "connectors", label: "Conn", fullLabel: "Connectors", shortcut: "C" },
];

// =============================================================================
// computeTabBarWidth
// =============================================================================

describe("computeTabBarWidth", () => {
  it("computes short label width for all 11 app tabs", () => {
    // Per tab: 4 (prefix) + label.length
    // Short labels: Files(5)+Ver(3)+Agent(5)+Zone(4)+ACL(3)+Pay(3)+Find(4)+Flow(4)+Event(5)+CLI(3)+Conn(4) = 43
    // Overhead: 11 × 4 = 44
    // Separators: 10 × 3 = 30
    // Total: 43 + 44 + 30 = 117
    expect(computeTabBarWidth(APP_TABS, false)).toBe(117);
  });

  it("computes full label width for all 11 app tabs", () => {
    // Full labels: Files(5)+Versions(8)+Agents(6)+Zones(5)+Access(6)+Payments(8)+Search(6)+Workflows(9)+Events(6)+Console(7)+Connectors(10) = 76
    // Overhead: 11 × 4 = 44
    // Separators: 10 × 3 = 30
    // Total: 76 + 44 + 30 = 150
    expect(computeTabBarWidth(APP_TABS, true)).toBe(150);
  });

  it("uses label as fallback when fullLabel is undefined", () => {
    const tabs: Tab[] = [
      { id: "a", label: "Foo", shortcut: "1" },
      { id: "b", label: "Bar", shortcut: "2" },
    ];
    // Both modes produce the same result since there's no fullLabel
    // Tab a: 4 + 3 = 7, Tab b: 4 + 3 = 7, separator: 3 → total: 17
    expect(computeTabBarWidth(tabs, true)).toBe(17);
    expect(computeTabBarWidth(tabs, false)).toBe(17);
  });

  it("handles a single tab (no separators)", () => {
    const tabs: Tab[] = [
      { id: "a", label: "X", fullLabel: "Extended", shortcut: "1" },
    ];
    expect(computeTabBarWidth(tabs, false)).toBe(5); // 4 + 1
    expect(computeTabBarWidth(tabs, true)).toBe(12); // 4 + 8
  });

  it("handles empty tabs array", () => {
    expect(computeTabBarWidth([], false)).toBe(0);
    expect(computeTabBarWidth([], true)).toBe(0);
  });

  it("handles mixed tabs with and without fullLabel", () => {
    const tabs: Tab[] = [
      { id: "a", label: "Hi", fullLabel: "Hello", shortcut: "1" },
      { id: "b", label: "Bye", shortcut: "2" }, // no fullLabel
    ];
    // Short: (4+2) + 3 + (4+3) = 16
    expect(computeTabBarWidth(tabs, false)).toBe(16);
    // Full: (4+5) + 3 + (4+3) = 19 — "b" falls back to "Bye"
    expect(computeTabBarWidth(tabs, true)).toBe(19);
  });
});

// =============================================================================
// shouldUseFullLabels
// =============================================================================

describe("shouldUseFullLabels", () => {
  it("returns true when columns exactly equals full label width", () => {
    expect(shouldUseFullLabels(APP_TABS, 150)).toBe(true);
  });

  it("returns true when columns exceeds full label width", () => {
    expect(shouldUseFullLabels(APP_TABS, 200)).toBe(true);
    expect(shouldUseFullLabels(APP_TABS, 250)).toBe(true);
  });

  it("returns false when columns is one less than full label width", () => {
    expect(shouldUseFullLabels(APP_TABS, 149)).toBe(false);
  });

  it("returns false at 120 columns (issue's original threshold)", () => {
    // This validates our finding: 120 is not wide enough for 11 full labels
    expect(shouldUseFullLabels(APP_TABS, 120)).toBe(false);
  });

  it("returns false at 80 columns (non-TTY fallback)", () => {
    expect(shouldUseFullLabels(APP_TABS, 80)).toBe(false);
  });

  it("returns false at 0 columns", () => {
    expect(shouldUseFullLabels(APP_TABS, 0)).toBe(false);
  });

  it("returns true for empty tabs at any width", () => {
    expect(shouldUseFullLabels([], 0)).toBe(true);
    expect(shouldUseFullLabels([], 80)).toBe(true);
  });

  it("adapts dynamically when tabs are added or removed", () => {
    const fewerTabs = APP_TABS.slice(0, 5);
    const fewerFullWidth = computeTabBarWidth(fewerTabs, true);
    // Fewer tabs → lower threshold → full labels at narrower terminals
    expect(shouldUseFullLabels(fewerTabs, fewerFullWidth)).toBe(true);
    expect(shouldUseFullLabels(fewerTabs, fewerFullWidth - 1)).toBe(false);
  });
});
