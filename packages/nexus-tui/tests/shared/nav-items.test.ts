/**
 * Tests for nav-items — panel navigation definitions (#3497).
 *
 * Covers:
 * - NAV_ITEMS: correct count, unique IDs, unique shortcuts
 * - All required fields are populated
 * - Icons are single characters for collapsed mode
 * - Panel IDs match the PanelId type exhaustively
 */

import { describe, it, expect } from "bun:test";
import { NAV_ITEMS, type NavItem } from "../../src/shared/nav-items.js";
import type { PanelId } from "../../src/stores/global-store.js";

// =============================================================================
// NAV_ITEMS structure
// =============================================================================

describe("NAV_ITEMS", () => {
  it("contains 12 items", () => {
    expect(NAV_ITEMS).toHaveLength(12);
  });

  it("has unique IDs", () => {
    const ids = NAV_ITEMS.map((item) => item.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("has unique shortcuts", () => {
    const shortcuts = NAV_ITEMS.map((item) => item.shortcut);
    expect(new Set(shortcuts).size).toBe(shortcuts.length);
  });

  it("covers all PanelId values", () => {
    const expectedIds: PanelId[] = [
      "files", "versions", "agents", "zones", "access", "payments",
      "search", "workflows", "infrastructure", "console", "connectors", "stack",
    ];
    const actualIds = NAV_ITEMS.map((item) => item.id);
    for (const id of expectedIds) {
      expect(actualIds).toContain(id);
    }
  });

  it("has non-empty labels for all items", () => {
    for (const item of NAV_ITEMS) {
      expect(item.label.length).toBeGreaterThan(0);
      expect(item.fullLabel.length).toBeGreaterThan(0);
    }
  });

  it("has single-character icons for collapsed mode", () => {
    for (const item of NAV_ITEMS) {
      // Icons should be exactly 1 visible character (may be multi-byte Unicode)
      expect(item.icon.length).toBeGreaterThan(0);
      // Spread to count grapheme clusters (handles multi-byte chars)
      expect([...item.icon]).toHaveLength(1);
    }
  });

  it("has shortcuts that are single characters", () => {
    for (const item of NAV_ITEMS) {
      expect(item.shortcut).toHaveLength(1);
    }
  });

  it("has fullLabel at least as long as label", () => {
    for (const item of NAV_ITEMS) {
      expect(item.fullLabel.length).toBeGreaterThanOrEqual(item.label.length);
    }
  });
});
