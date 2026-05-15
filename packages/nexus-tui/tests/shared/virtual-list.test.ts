/**
 * Tests for the calculateWindow pure function used by VirtualList.
 *
 * Validates scroll offset calculation, viewport windowing, and overscan
 * buffer clamping without involving React rendering.
 *
 * @see Issue #3102, Decision 1A
 */

import { describe, it, expect } from "bun:test";
import { calculateWindow } from "../../src/shared/components/virtual-list.js";

describe("calculateWindow", () => {
  it("returns zeros for an empty list", () => {
    const result = calculateWindow(0, 10, 0, 2);
    expect(result).toEqual({ startIndex: 0, endIndex: 0, scrollOffset: 0 });
  });

  it("handles a single item", () => {
    const result = calculateWindow(1, 10, 0, 2);
    expect(result.startIndex).toBe(0);
    expect(result.endIndex).toBe(1);
    expect(result.scrollOffset).toBe(0);
  });

  it("shows all items when count equals viewport height", () => {
    // totalItems=10, viewportHeight=10 => everything fits, no scroll
    const result = calculateWindow(10, 10, 5, 2);
    expect(result.scrollOffset).toBe(0);
    expect(result.startIndex).toBe(0);
    // endIndex = min(10, 0 + 10 + 2) = 10
    expect(result.endIndex).toBe(10);
  });

  it("shows all items when count is less than viewport height", () => {
    const result = calculateWindow(5, 10, 3, 2);
    expect(result.scrollOffset).toBe(0);
    expect(result.startIndex).toBe(0);
    expect(result.endIndex).toBe(5);
  });

  it("clamps scroll offset to 0 when selectedIndex is at top", () => {
    // 100 items, viewport 20, selected at 0 => offset centers to max(0, 0-10) = 0
    const result = calculateWindow(100, 20, 0, 3);
    expect(result.scrollOffset).toBe(0);
    // startIndex = max(0, 0-3) = 0
    expect(result.startIndex).toBe(0);
    // endIndex = min(100, 0+20+3) = 23
    expect(result.endIndex).toBe(23);
  });

  it("centers selectedIndex in the viewport for a large list", () => {
    // 100 items, viewport 20, selected at 50
    // scrollOffset = max(0, 50 - floor(20/2)) = 50 - 10 = 40
    // maxOffset = 100 - 20 = 80, so 40 is within range
    const result = calculateWindow(100, 20, 50, 3);
    expect(result.scrollOffset).toBe(40);
    // startIndex = max(0, 40-3) = 37
    expect(result.startIndex).toBe(37);
    // endIndex = min(100, 40+20+3) = 63
    expect(result.endIndex).toBe(63);
  });

  it("clamps scroll offset to maxOffset when selectedIndex is at the end", () => {
    // 100 items, viewport 20, selected at 99
    // scrollOffset = max(0, 99 - 10) = 89, but maxOffset = 100-20 = 80
    // so scrollOffset = 80
    const result = calculateWindow(100, 20, 99, 3);
    expect(result.scrollOffset).toBe(80);
    // startIndex = max(0, 80-3) = 77
    expect(result.startIndex).toBe(77);
    // endIndex = min(100, 80+20+3) = 100
    expect(result.endIndex).toBe(100);
  });

  it("clamps overscan at the top edge so startIndex never goes negative", () => {
    // 50 items, viewport 10, selected at 2, overscan 8
    // scrollOffset = max(0, 2 - 5) = 0
    // startIndex = max(0, 0 - 8) = 0 (not -8)
    const result = calculateWindow(50, 10, 2, 8);
    expect(result.scrollOffset).toBe(0);
    expect(result.startIndex).toBe(0);
    // endIndex = min(50, 0+10+8) = 18
    expect(result.endIndex).toBe(18);
  });

  it("clamps overscan at the bottom edge so endIndex never exceeds totalItems", () => {
    // 50 items, viewport 10, selected at 47, overscan 8
    // scrollOffset = max(0, 47 - 5) = 42, maxOffset = 50-10 = 40 => 40
    // endIndex = min(50, 40+10+8) = min(50, 58) = 50
    const result = calculateWindow(50, 10, 47, 8);
    expect(result.scrollOffset).toBe(40);
    expect(result.endIndex).toBe(50);
    // startIndex = max(0, 40-8) = 32
    expect(result.startIndex).toBe(32);
  });

  it("handles overscan larger than totalItems", () => {
    // 5 items, viewport 3, selected at 2, overscan 100
    // scrollOffset = max(0, 2 - 1) = 1, maxOffset = 5-3 = 2 => 1
    // startIndex = max(0, 1 - 100) = 0
    // endIndex = min(5, 1 + 3 + 100) = 5
    const result = calculateWindow(5, 3, 2, 100);
    expect(result.scrollOffset).toBe(1);
    expect(result.startIndex).toBe(0);
    expect(result.endIndex).toBe(5);
  });
});
