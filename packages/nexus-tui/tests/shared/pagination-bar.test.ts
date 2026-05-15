/**
 * Tests for PaginationBar display logic.
 * Tests the exported formatPageDisplay function (real production code).
 */

import { describe, it, expect } from "bun:test";
import { formatPageDisplay } from "../../src/shared/components/pagination-bar.js";

describe("formatPageDisplay", () => {
  it("shows page X of Y when totalPages is known", () => {
    expect(formatPageDisplay(3, false, 12)).toBe("Page 3 of 12");
  });

  it("shows page X+ when totalPages unknown but hasMore", () => {
    expect(formatPageDisplay(3, true)).toBe("Page 3+");
  });

  it("shows page X without + when on last page", () => {
    expect(formatPageDisplay(5, false)).toBe("Page 5");
  });

  it("shows page 1 on first page", () => {
    expect(formatPageDisplay(1, true)).toBe("Page 1+");
  });
});
