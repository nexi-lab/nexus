/**
 * Tab cycling tests for access-panel keybinding logic (#3623).
 *
 * These tests operate directly on subTabForward / subTabBackward and the
 * panel-level override logic to prevent the shift+tab copy-paste bug
 * (Issue #3623 P0) from regressing silently.
 *
 * The panel uses:
 *   - subTabCycleBindings spread (provides base tab + shift+tab)
 *   - A `tab:` override for the fraud sub-pane toggle
 *   - No shift+tab override (the spread handles backward cycling correctly)
 */

import { describe, it, expect, mock } from "bun:test";
import {
  subTabForward,
  subTabBackward,
  subTabCycleBindings,
} from "../../src/shared/components/sub-tab-bar-utils.js";
import type { AccessTab } from "../../src/stores/access-store.js";

// =============================================================================
// Test data — matches ACCESS_TABS in navigation.ts (brick conditions ignored)
// =============================================================================

const ACCESS_TABS = [
  { id: "manifests",   label: "Manifests"   },
  { id: "alerts",      label: "Alerts"      },
  { id: "credentials", label: "Credentials" },
  { id: "fraud",       label: "Fraud"       },
  { id: "delegations", label: "Delegations" },
] as const;

type TestAccessTab = (typeof ACCESS_TABS)[number]["id"];

// =============================================================================
// Tab forward cycling (the `tab` key — non-fraud case)
// =============================================================================

describe("access panel tab — forward cycling (non-fraud tabs)", () => {
  it("manifests → alerts", () => {
    let active: TestAccessTab = "manifests";
    subTabForward(ACCESS_TABS, active, (t) => { active = t; });
    expect(active).toBe("alerts");
  });

  it("alerts → credentials", () => {
    let active: TestAccessTab = "alerts";
    subTabForward(ACCESS_TABS, active, (t) => { active = t; });
    expect(active).toBe("credentials");
  });

  it("delegations wraps to manifests", () => {
    let active: TestAccessTab = "delegations";
    subTabForward(ACCESS_TABS, active, (t) => { active = t; });
    expect(active).toBe("manifests");
  });
});

// =============================================================================
// Tab backward cycling (the `shift+tab` key — must go BACKWARD, not forward)
// =============================================================================

describe("access panel shift+tab — backward cycling", () => {
  it("alerts → manifests", () => {
    let active: TestAccessTab = "alerts";
    subTabBackward(ACCESS_TABS, active, (t) => { active = t; });
    expect(active).toBe("manifests");
  });

  it("credentials → alerts", () => {
    let active: TestAccessTab = "credentials";
    subTabBackward(ACCESS_TABS, active, (t) => { active = t; });
    expect(active).toBe("alerts");
  });

  it("manifests wraps to delegations", () => {
    let active: TestAccessTab = "manifests";
    subTabBackward(ACCESS_TABS, active, (t) => { active = t; });
    expect(active).toBe("delegations");
  });

  it("shift+tab goes BACKWARD — not the same direction as tab (regression guard)", () => {
    // This test explicitly guards against the copy-paste bug where shift+tab
    // used (currentIdx + 1) instead of (currentIdx - 1 + length).
    let forwardActive: TestAccessTab = "alerts";
    let backwardActive: TestAccessTab = "alerts";

    subTabForward(ACCESS_TABS, forwardActive, (t) => { forwardActive = t; });
    subTabBackward(ACCESS_TABS, backwardActive, (t) => { backwardActive = t; });

    // Forward: alerts → credentials; backward: alerts → manifests
    expect(forwardActive).toBe("credentials");
    expect(backwardActive).toBe("manifests");
    expect(forwardActive).not.toBe(backwardActive);
  });
});

// =============================================================================
// subTabCycleBindings spread — verifies both tab and shift+tab are correct
// =============================================================================

describe("subTabCycleBindings for access panel", () => {
  it("tab binding cycles forward from manifests", () => {
    let active: TestAccessTab = "manifests";
    const bindings = subTabCycleBindings(ACCESS_TABS, active, (t) => { active = t; });
    bindings["tab"]!();
    expect(active).toBe("alerts");
  });

  it("shift+tab binding cycles BACKWARD from credentials", () => {
    let active: TestAccessTab = "credentials";
    const bindings = subTabCycleBindings(ACCESS_TABS, active, (t) => { active = t; });
    bindings["shift+tab"]!();
    expect(active).toBe("alerts"); // backward, not forward to fraud
  });

  it("tab and shift+tab produce opposite results from the same position", () => {
    let afterTab: TestAccessTab = "alerts";
    let afterShiftTab: TestAccessTab = "alerts";

    subTabCycleBindings(ACCESS_TABS, "alerts", (t) => { afterTab = t; })["tab"]!();
    subTabCycleBindings(ACCESS_TABS, "alerts", (t) => { afterShiftTab = t; })["shift+tab"]!();

    expect(afterTab).not.toBe(afterShiftTab);
  });
});

// =============================================================================
// Fraud tab: `tab` override toggles sub-pane focus, does NOT cycle tabs
// =============================================================================

describe("access panel fraud tab — tab toggles sub-pane focus", () => {
  it("tab on fraud tab toggles focus, does not call setActiveTab", () => {
    // Simulate the panel's tab override for the fraud case:
    //   if (activeTab === "fraud") { setFraudFocus(...); return; }
    //   else subTabForward(...)
    let active: TestAccessTab = "fraud";
    const setActiveTab = mock(() => {});
    let fraudFocus: "scores" | "constraints" = "scores";

    // Panel tab handler logic
    const handleTab = (): void => {
      if (active === "fraud") {
        fraudFocus = fraudFocus === "scores" ? "constraints" : "scores";
        return;
      }
      subTabForward(ACCESS_TABS, active, setActiveTab);
    };

    handleTab();
    expect(fraudFocus).toBe("constraints");
    expect(setActiveTab).not.toHaveBeenCalled();

    handleTab();
    expect(fraudFocus).toBe("scores");
    expect(setActiveTab).not.toHaveBeenCalled();
  });

  it("tab on non-fraud tabs advances to next tab", () => {
    let active: TestAccessTab = "manifests";
    const setActiveTab = mock(() => {});
    let fraudFocus: "scores" | "constraints" = "scores";

    const handleTab = (): void => {
      if (active === "fraud") {
        fraudFocus = fraudFocus === "scores" ? "constraints" : "scores";
        return;
      }
      subTabForward(ACCESS_TABS, active, setActiveTab);
    };

    handleTab();
    expect(setActiveTab).toHaveBeenCalledWith("alerts");
    expect(fraudFocus).toBe("scores"); // not changed
  });
});

// =============================================================================
// Wrap-around edge cases
// =============================================================================

describe("tab cycling wrap-around", () => {
  it("forward from last tab wraps to first", () => {
    let active: TestAccessTab = "delegations";
    subTabForward(ACCESS_TABS, active, (t) => { active = t; });
    expect(active).toBe("manifests");
  });

  it("backward from first tab wraps to last", () => {
    let active: TestAccessTab = "manifests";
    subTabBackward(ACCESS_TABS, active, (t) => { active = t; });
    expect(active).toBe("delegations");
  });

  it("forward wrap and backward wrap are inverses", () => {
    // Starting at manifests, go forward then immediately back = manifests
    let active: TestAccessTab = "manifests";
    const calls: TestAccessTab[] = [];

    subTabForward(ACCESS_TABS, active, (t) => { active = t; calls.push(t); });
    subTabBackward(ACCESS_TABS, active, (t) => { active = t; calls.push(t); });

    expect(calls[1]).toBe("manifests");
  });
});
