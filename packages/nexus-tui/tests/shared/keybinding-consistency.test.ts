/**
 * Keybinding consistency integration tests.
 *
 * Verifies that standardized keys (d, n, x) have consistent semantics
 * across all panels using the REAL keybinding registry from help-overlay.
 *
 * @see Issue #3066, Decision 12A
 */

import { describe, it, expect } from "bun:test";
import { PANEL_BINDINGS, type KeyBinding } from "../../src/shared/action-registry.js";
import { ALL_PANEL_IDS } from "../../src/shared/nav-items.js";
import {
  jumpToStart,
  jumpToEnd,
  moveIndex,
  clampIndex,
  selectionPrefix,
  listNavigationBindings,
} from "../../src/shared/hooks/use-list-navigation.js";

// =============================================================================
// Helper: find all bindings for a given key across all panels
// =============================================================================

function findBindingsForKey(key: string): Array<{ panel: string; action: string }> {
  const results: Array<{ panel: string; action: string }> = [];
  for (const [panel, bindings] of Object.entries(PANEL_BINDINGS)) {
    for (const binding of bindings) {
      if (binding.key.toLowerCase() === key.toLowerCase()) {
        results.push({ panel, action: binding.action.toLowerCase() });
      }
    }
  }
  return results;
}

describe("keybinding consistency (from real registry)", () => {
  describe("standardized key semantics", () => {
    it("d should only be used for destructive actions", () => {
      const dBindings = findBindingsForKey("d");
      expect(dBindings.length).toBeGreaterThan(0);
      for (const { panel, action } of dBindings) {
        const isDestructive =
          action.includes("delete") ||
          action.includes("revoke") ||
          action.includes("unregister") ||
          action.includes("release");
        expect(isDestructive).toBe(true);
      }
    });

    it("n should only be used for creation actions", () => {
      const nBindings = findBindingsForKey("n");
      expect(nBindings.length).toBeGreaterThan(0);
      for (const { panel, action } of nBindings) {
        const isCreation =
          action.includes("new") ||
          action.includes("create") ||
          action.includes("register") ||
          action.includes("acquire");
        expect(isCreation).toBe(true);
      }
    });

    it("x should only be used for revoke/cancel/cut actions", () => {
      const xBindings = findBindingsForKey("x");
      expect(xBindings.length).toBeGreaterThan(0);
      for (const { panel, action } of xBindings) {
        const isAllowed =
          action.includes("revoke") ||
          action.includes("release") ||
          action.includes("reset") ||
          action.includes("reject") ||
          action.includes("cut");
        expect(isAllowed).toBe(true);
      }
    });

    it("no panel uses d for non-destructive actions", () => {
      const dBindings = findBindingsForKey("d");
      for (const { action } of dBindings) {
        // "diff" and "view" are NOT destructive — should not use d
        expect(action.includes("diff")).toBe(false);
        expect(action.includes("view")).toBe(false);
      }
    });
  });

  describe("navigation helpers (g/G)", () => {
    it("g (jumpToStart) always returns 0", () => {
      expect(jumpToStart()).toBe(0);
    });

    it("G (jumpToEnd) returns last index", () => {
      expect(jumpToEnd(10)).toBe(9);
      expect(jumpToEnd(1)).toBe(0);
      expect(jumpToEnd(0)).toBe(0);
    });

    it("j/k (moveIndex) clamps correctly", () => {
      expect(moveIndex(0, 1, 10)).toBe(1);
      expect(moveIndex(0, -1, 10)).toBe(0);
      expect(moveIndex(9, 1, 10)).toBe(9);
      expect(moveIndex(0, 1, 0)).toBe(0);
    });

    it("clampIndex handles all edge cases", () => {
      expect(clampIndex(-5, 10)).toBe(0);
      expect(clampIndex(15, 10)).toBe(9);
      expect(clampIndex(5, 0)).toBe(0);
    });

    it("selectionPrefix renders correctly", () => {
      expect(selectionPrefix(3, 3)).toBe("> ");
      expect(selectionPrefix(3, 5)).toBe("  ");
    });
  });

  describe("listNavigationBindings builder", () => {
    it("generates standard keybindings", () => {
      let idx = 0;
      const bindings = listNavigationBindings({
        getIndex: () => idx,
        setIndex: (i) => { idx = i; },
        getLength: () => 5,
      });

      expect(bindings["j"]).toBeDefined();
      expect(bindings["k"]).toBeDefined();
      expect(bindings["down"]).toBeDefined();
      expect(bindings["up"]).toBeDefined();
      expect(bindings["shift+g"]).toBeDefined();
    });

    it("j moves down", () => {
      let idx = 0;
      const bindings = listNavigationBindings({
        getIndex: () => idx,
        setIndex: (i) => { idx = i; },
        getLength: () => 5,
      });
      bindings["j"]!();
      expect(idx).toBe(1);
    });

    it("shift+g jumps to end", () => {
      let idx = 0;
      const bindings = listNavigationBindings({
        getIndex: () => idx,
        setIndex: (i) => { idx = i; },
        getLength: () => 10,
      });
      bindings["shift+g"]!();
      expect(idx).toBe(9);
    });

    it("includes Enter handler when onSelect provided", () => {
      let selected = -1;
      const bindings = listNavigationBindings({
        getIndex: () => 3,
        setIndex: () => {},
        getLength: () => 5,
        onSelect: (i) => { selected = i; },
      });
      expect(bindings["return"]).toBeDefined();
      bindings["return"]!();
      expect(selected).toBe(3);
    });

    it("omits Enter when onSelect not provided", () => {
      const bindings = listNavigationBindings({
        getIndex: () => 0,
        setIndex: () => {},
        getLength: () => 5,
      });
      expect(bindings["return"]).toBeUndefined();
    });
  });

  describe("registry completeness", () => {
    it("every PanelId has keybinding entries in PANEL_BINDINGS", () => {
      // Derived from ALL_PANEL_IDS so this test auto-updates when PanelId grows.
      for (const panel of ALL_PANEL_IDS) {
        expect(PANEL_BINDINGS[panel]).toBeDefined();
        expect(PANEL_BINDINGS[panel]!.length).toBeGreaterThan(0);
      }
    });
  });
});
