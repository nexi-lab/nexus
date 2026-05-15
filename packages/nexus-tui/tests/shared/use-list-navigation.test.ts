/**
 * Tests for useListNavigation — shared list cursor logic.
 *
 * Written test-first (Decision 10A).
 * Tests the pure state logic (not React hooks) via the store directly.
 */

import { describe, it, expect } from "bun:test";
import {
  clampIndex,
  moveIndex,
  jumpToStart,
  jumpToEnd,
} from "../../src/shared/hooks/use-list-navigation.js";

describe("list navigation helpers", () => {
  describe("clampIndex", () => {
    it("returns 0 for empty list", () => {
      expect(clampIndex(5, 0)).toBe(0);
    });

    it("clamps negative to 0", () => {
      expect(clampIndex(-1, 10)).toBe(0);
    });

    it("clamps over length to length - 1", () => {
      expect(clampIndex(15, 10)).toBe(9);
    });

    it("returns valid index unchanged", () => {
      expect(clampIndex(3, 10)).toBe(3);
    });

    it("handles single-item list", () => {
      expect(clampIndex(0, 1)).toBe(0);
      expect(clampIndex(1, 1)).toBe(0);
    });
  });

  describe("moveIndex", () => {
    it("moves down by 1", () => {
      expect(moveIndex(0, 1, 10)).toBe(1);
    });

    it("moves up by 1", () => {
      expect(moveIndex(5, -1, 10)).toBe(4);
    });

    it("clamps at bottom", () => {
      expect(moveIndex(9, 1, 10)).toBe(9);
    });

    it("clamps at top", () => {
      expect(moveIndex(0, -1, 10)).toBe(0);
    });

    it("handles empty list", () => {
      expect(moveIndex(0, 1, 0)).toBe(0);
    });

    it("handles large delta", () => {
      expect(moveIndex(0, 100, 10)).toBe(9);
    });
  });

  describe("jumpToStart", () => {
    it("returns 0", () => {
      expect(jumpToStart()).toBe(0);
    });
  });

  describe("jumpToEnd", () => {
    it("returns last index", () => {
      expect(jumpToEnd(10)).toBe(9);
    });

    it("returns 0 for empty list", () => {
      expect(jumpToEnd(0)).toBe(0);
    });

    it("returns 0 for single-item list", () => {
      expect(jumpToEnd(1)).toBe(0);
    });
  });
});
