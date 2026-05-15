/**
 * Tests for SideNav layout utility functions (#3497).
 *
 * Covers:
 * - getSideNavMode: responsive breakpoint calculation
 * - getSideNavWidth: width for each display mode
 * - Constants: threshold values and sidebar widths
 * - Edge cases: boundary columns, zero width
 */

import { describe, it, expect } from "bun:test";
import {
  getSideNavMode,
  getSideNavWidth,
  FULL_THRESHOLD,
  COLLAPSED_THRESHOLD,
  TERMINAL_GUARD_MIN_COLS,
  TERMINAL_GUARD_MIN_ROWS,
  RESIZE_DEBOUNCE_MS,
  SIDE_NAV_FULL_WIDTH,
  SIDE_NAV_COLLAPSED_WIDTH,
} from "../../src/shared/components/side-nav-utils.js";

// =============================================================================
// getSideNavMode
// =============================================================================

describe("getSideNavMode", () => {
  it("returns 'full' at the exact full threshold", () => {
    expect(getSideNavMode(FULL_THRESHOLD)).toBe("full");
  });

  it("returns 'full' above the full threshold", () => {
    expect(getSideNavMode(200)).toBe("full");
    expect(getSideNavMode(300)).toBe("full");
  });

  it("returns 'collapsed' one below the full threshold", () => {
    expect(getSideNavMode(FULL_THRESHOLD - 1)).toBe("collapsed");
  });

  it("returns 'collapsed' at the exact collapsed threshold", () => {
    expect(getSideNavMode(COLLAPSED_THRESHOLD)).toBe("collapsed");
  });

  it("returns 'collapsed' between collapsed and full thresholds", () => {
    expect(getSideNavMode(100)).toBe("collapsed");
  });

  it("returns 'hidden' one below the collapsed threshold", () => {
    expect(getSideNavMode(COLLAPSED_THRESHOLD - 1)).toBe("hidden");
  });

  it("returns 'hidden' at very small terminal widths", () => {
    expect(getSideNavMode(40)).toBe("hidden");
    expect(getSideNavMode(1)).toBe("hidden");
  });

  it("returns 'hidden' at zero width", () => {
    expect(getSideNavMode(0)).toBe("hidden");
  });
});

// =============================================================================
// getSideNavWidth
// =============================================================================

describe("getSideNavWidth", () => {
  it("returns full width for 'full' mode", () => {
    expect(getSideNavWidth("full")).toBe(SIDE_NAV_FULL_WIDTH);
  });

  it("returns collapsed width for 'collapsed' mode", () => {
    expect(getSideNavWidth("collapsed")).toBe(SIDE_NAV_COLLAPSED_WIDTH);
  });

  it("returns 0 for 'hidden' mode", () => {
    expect(getSideNavWidth("hidden")).toBe(0);
  });
});

// =============================================================================
// Constants
// =============================================================================

describe("thresholds", () => {
  it("full threshold is 120", () => {
    expect(FULL_THRESHOLD).toBe(120);
  });

  it("collapsed threshold is 80", () => {
    expect(COLLAPSED_THRESHOLD).toBe(80);
  });

  it("terminal guard min cols is 60", () => {
    expect(TERMINAL_GUARD_MIN_COLS).toBe(60);
  });

  it("terminal guard min rows is 24", () => {
    expect(TERMINAL_GUARD_MIN_ROWS).toBe(24);
  });

  it("resize debounce is 150 ms", () => {
    expect(RESIZE_DEBOUNCE_MS).toBe(150);
  });

  // 3-way ordering invariant: guard < collapsed < full.
  // If any constant is changed incorrectly, these catch it.
  it("guard min cols < collapsed threshold < full threshold", () => {
    expect(TERMINAL_GUARD_MIN_COLS).toBeLessThan(COLLAPSED_THRESHOLD);
    expect(COLLAPSED_THRESHOLD).toBeLessThan(FULL_THRESHOLD);
  });
});

describe("widths", () => {
  it("full width is 18", () => {
    expect(SIDE_NAV_FULL_WIDTH).toBe(18);
  });

  it("collapsed width is 6", () => {
    expect(SIDE_NAV_COLLAPSED_WIDTH).toBe(6);
  });

  it("full width is greater than collapsed width", () => {
    expect(SIDE_NAV_FULL_WIDTH).toBeGreaterThan(SIDE_NAV_COLLAPSED_WIDTH);
  });
});
