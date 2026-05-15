/**
 * Tests for shared formatSize utility.
 * @see Issue #3102, Decision 5A
 */

import { describe, it, expect } from "bun:test";
import { formatSize } from "../../src/shared/utils/format-size.js";

describe("formatSize", () => {
  it("formats bytes", () => {
    expect(formatSize(0)).toBe("0 B");
    expect(formatSize(1)).toBe("1 B");
    expect(formatSize(512)).toBe("512 B");
    expect(formatSize(1023)).toBe("1023 B");
  });

  it("formats kilobytes", () => {
    expect(formatSize(1024)).toBe("1.0 KB");
    expect(formatSize(1536)).toBe("1.5 KB");
    expect(formatSize(10240)).toBe("10.0 KB");
  });

  it("formats megabytes", () => {
    expect(formatSize(1024 * 1024)).toBe("1.0 MB");
    expect(formatSize(1024 * 1024 * 2.5)).toBe("2.5 MB");
  });

  it("formats gigabytes", () => {
    expect(formatSize(1024 * 1024 * 1024)).toBe("1.0 GB");
    expect(formatSize(1024 * 1024 * 1024 * 3.7)).toBe("3.7 GB");
  });

  it("handles boundary values", () => {
    // Just below KB threshold
    expect(formatSize(1023)).toBe("1023 B");
    // Exactly at KB threshold
    expect(formatSize(1024)).toBe("1.0 KB");
    // Just below MB threshold
    expect(formatSize(1024 * 1024 - 1)).toBe("1024.0 KB");
    // Exactly at MB threshold
    expect(formatSize(1024 * 1024)).toBe("1.0 MB");
  });
});
