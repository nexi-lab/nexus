/**
 * Tests for shared truncateText utility.
 * @see Issue #3102, Decision 8A
 */

import { describe, it, expect } from "bun:test";
import { truncateText } from "../../src/shared/utils/format-text.js";

describe("truncateText", () => {
  it("returns text unchanged when within limit", () => {
    expect(truncateText("hello", 10)).toBe("hello");
    expect(truncateText("abc", 3)).toBe("abc");
    expect(truncateText("", 5)).toBe("");
  });

  it("truncates with ellipsis when exceeding limit", () => {
    expect(truncateText("hello world", 8)).toBe("hello...");
    expect(truncateText("abcdefghij", 7)).toBe("abcd...");
  });

  it("handles exact boundary", () => {
    expect(truncateText("12345", 5)).toBe("12345");
    expect(truncateText("123456", 5)).toBe("12...");
  });

  it("handles small maxLen", () => {
    expect(truncateText("hello", 3)).toBe("...");
    expect(truncateText("hello", 4)).toBe("h...");
  });
});
