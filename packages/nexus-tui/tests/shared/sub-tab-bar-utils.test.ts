/**
 * Tests for sub-tab bar utility functions (#3498).
 *
 * Covers:
 * - subTabForward: forward cycling with wrap, activeTab-not-in-list guard
 * - subTabBackward: backward cycling with wrap, activeTab-not-in-list guard
 * - tabFallback: fallback to first visible tab when active tab becomes hidden
 */

import { describe, it, expect } from "bun:test";
import {
  subTabForward,
  subTabBackward,
  subTabCycleBindings,
  tabFallback,
} from "../../src/shared/components/sub-tab-bar-utils.js";

// =============================================================================
// Test data
// =============================================================================

const TABS = [
  { id: "alpha", label: "Alpha" },
  { id: "beta", label: "Beta" },
  { id: "gamma", label: "Gamma" },
  { id: "delta", label: "Delta" },
] as const;

type TestTab = (typeof TABS)[number]["id"];

// =============================================================================
// subTabForward
// =============================================================================

describe("subTabForward", () => {
  it("cycles to the next tab", () => {
    let active: TestTab = "alpha";
    subTabForward(TABS, active, (t) => { active = t; });
    expect(active).toBe("beta");
  });

  it("wraps from last to first", () => {
    let active: TestTab = "delta";
    subTabForward(TABS, active, (t) => { active = t; });
    expect(active).toBe("alpha");
  });

  it("stays on same tab when only one tab", () => {
    let active = "only";
    subTabForward([{ id: "only", label: "Only" }], active, (t) => { active = t; });
    expect(active).toBe("only");
  });

  it("does nothing with empty tabs", () => {
    let active = "any";
    subTabForward([], active, (t) => { active = t; });
    expect(active).toBe("any");
  });

  it("defaults to first tab when activeTab not in list", () => {
    let active = "missing" as string;
    subTabForward(TABS, active, (t) => { active = t; });
    expect(active).toBe("alpha");
  });

  it("cycles through middle tabs", () => {
    let active: TestTab = "beta";
    subTabForward(TABS, active, (t) => { active = t; });
    expect(active).toBe("gamma");
  });
});

// =============================================================================
// subTabBackward
// =============================================================================

describe("subTabBackward", () => {
  it("cycles to the previous tab", () => {
    let active: TestTab = "beta";
    subTabBackward(TABS, active, (t) => { active = t; });
    expect(active).toBe("alpha");
  });

  it("wraps from first to last", () => {
    let active: TestTab = "alpha";
    subTabBackward(TABS, active, (t) => { active = t; });
    expect(active).toBe("delta");
  });

  it("stays on same tab when only one tab", () => {
    let active = "only";
    subTabBackward([{ id: "only", label: "Only" }], active, (t) => { active = t; });
    expect(active).toBe("only");
  });

  it("does nothing with empty tabs", () => {
    let active = "any";
    subTabBackward([], active, (t) => { active = t; });
    expect(active).toBe("any");
  });

  it("defaults to first tab when activeTab not in list", () => {
    let active = "missing" as string;
    subTabBackward(TABS, active, (t) => { active = t; });
    expect(active).toBe("alpha");
  });

  it("cycles through middle tabs", () => {
    let active: TestTab = "gamma";
    subTabBackward(TABS, active, (t) => { active = t; });
    expect(active).toBe("beta");
  });
});

// =============================================================================
// subTabCycleBindings
// =============================================================================

describe("subTabCycleBindings", () => {
  it("returns tab and shift+tab bindings", () => {
    const bindings = subTabCycleBindings(TABS, "alpha", () => {});
    expect(bindings["tab"]).toBeDefined();
    expect(bindings["shift+tab"]).toBeDefined();
  });

  it("tab binding cycles forward", () => {
    let active: TestTab = "alpha";
    const bindings = subTabCycleBindings(TABS, active, (t) => { active = t; });
    bindings["tab"]!();
    expect(active).toBe("beta");
  });

  it("shift+tab binding cycles backward", () => {
    let active: TestTab = "beta";
    const bindings = subTabCycleBindings(TABS, active, (t) => { active = t; });
    bindings["shift+tab"]!();
    expect(active).toBe("alpha");
  });

  it("can be spread and overridden for split-pane panels", () => {
    let active: TestTab = "alpha";
    let focusToggled = false;
    const bindings = {
      ...subTabCycleBindings(TABS, active, (t) => { active = t; }),
      "shift+tab": () => { focusToggled = true; },
    };
    bindings["shift+tab"]!();
    expect(focusToggled).toBe(true);
    expect(active).toBe("alpha"); // tab cycling was overridden
  });
});

// =============================================================================
// tabFallback
// =============================================================================

describe("tabFallback", () => {
  it("returns null when activeTab is visible", () => {
    expect(tabFallback(["alpha", "beta", "gamma"], "beta")).toBeNull();
  });

  it("returns first visible tab when activeTab is not visible", () => {
    expect(tabFallback(["beta", "gamma"], "alpha")).toBe("beta");
  });

  it("returns null for empty visible list", () => {
    expect(tabFallback([], "alpha")).toBeNull();
  });

  it("returns the single visible tab when activeTab differs", () => {
    expect(tabFallback(["beta"], "alpha")).toBe("beta");
  });

  it("returns null when single visible tab matches activeTab", () => {
    expect(tabFallback(["alpha"], "alpha")).toBeNull();
  });

  it("returns first visible tab when activeTab was removed", () => {
    // Simulates a brick being disabled that hides the active tab
    expect(tabFallback(["gamma", "delta"], "beta")).toBe("gamma");
  });
});
