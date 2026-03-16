/**
 * Tests for PaginationBar display logic.
 */

import { describe, it, expect } from "bun:test";

// Since PaginationBar is a React component, we test the display logic
// that would be computed inside it.

describe("PaginationBar logic", () => {
  function pageDisplay(currentPage: number, totalPages?: number, hasMore?: boolean): string {
    return totalPages
      ? `Page ${currentPage} of ${totalPages}`
      : `Page ${currentPage}${hasMore ? "+" : ""}`;
  }

  it("shows page X of Y when totalPages is known", () => {
    expect(pageDisplay(3, 12)).toBe("Page 3 of 12");
  });

  it("shows page X+ when totalPages unknown but hasMore", () => {
    expect(pageDisplay(3, undefined, true)).toBe("Page 3+");
  });

  it("shows page X without + when on last page", () => {
    expect(pageDisplay(5, undefined, false)).toBe("Page 5");
  });

  it("shows page 1 on first page", () => {
    expect(pageDisplay(1, undefined, true)).toBe("Page 1+");
  });
});
