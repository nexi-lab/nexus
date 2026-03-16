import { describe, it, expect } from "bun:test";
import { formatTimestamp } from "../../src/shared/utils/format-time.js";

// Fixed "now" for deterministic tests: 2026-03-15T12:00:00.000Z
const NOW = new Date("2026-03-15T12:00:00.000Z").getTime();

describe("formatTimestamp", () => {
  describe("relative times (< 24 hours)", () => {
    it("returns 'just now' for < 2 seconds", () => {
      expect(formatTimestamp(NOW - 500, NOW)).toBe("just now");
      expect(formatTimestamp(NOW - 1_000, NOW)).toBe("just now");
    });

    it("returns seconds ago for < 1 minute", () => {
      expect(formatTimestamp(NOW - 5_000, NOW)).toBe("5s ago");
      expect(formatTimestamp(NOW - 30_000, NOW)).toBe("30s ago");
      expect(formatTimestamp(NOW - 59_000, NOW)).toBe("59s ago");
    });

    it("returns minutes ago for < 1 hour", () => {
      expect(formatTimestamp(NOW - 60_000, NOW)).toBe("1m ago");
      expect(formatTimestamp(NOW - 300_000, NOW)).toBe("5m ago");
      expect(formatTimestamp(NOW - 3_540_000, NOW)).toBe("59m ago");
    });

    it("returns hours ago for < 24 hours", () => {
      expect(formatTimestamp(NOW - 3_600_000, NOW)).toBe("1h ago");
      expect(formatTimestamp(NOW - 7_200_000, NOW)).toBe("2h ago");
      expect(formatTimestamp(NOW - 82_800_000, NOW)).toBe("23h ago");
    });
  });

  describe("absolute times (>= 24 hours)", () => {
    it("formats dates older than 24 hours", () => {
      // 2 days ago = 2026-03-13T12:00:00Z
      const result = formatTimestamp(NOW - 2 * 86_400_000, NOW);
      expect(result).toContain("Mar");
      expect(result).toContain("13");
      expect(result).toContain("12:00");
    });

    it("pads single-digit days with space", () => {
      const jan5 = new Date("2026-01-05T09:30:00.000Z").getTime();
      const result = formatTimestamp(jan5, NOW);
      expect(result).toBe("Jan  5, 09:30");
    });

    it("pads hours and minutes with zeros", () => {
      const earlyMorning = new Date("2026-01-15T03:05:00.000Z").getTime();
      const result = formatTimestamp(earlyMorning, NOW);
      expect(result).toBe("Jan 15, 03:05");
    });
  });

  describe("input types", () => {
    it("accepts number (epoch ms)", () => {
      expect(formatTimestamp(NOW - 5_000, NOW)).toBe("5s ago");
    });

    it("accepts ISO string", () => {
      const iso = new Date(NOW - 5_000).toISOString();
      expect(formatTimestamp(iso, NOW)).toBe("5s ago");
    });

    it("accepts Date object", () => {
      const date = new Date(NOW - 5_000);
      expect(formatTimestamp(date, NOW)).toBe("5s ago");
    });
  });

  describe("edge cases", () => {
    it("returns — for invalid input", () => {
      expect(formatTimestamp(NaN)).toBe("—");
      expect(formatTimestamp("not a date")).toBe("—");
    });

    it("shows absolute time for future timestamps", () => {
      const future = NOW + 86_400_000;
      const result = formatTimestamp(future, NOW);
      expect(result).toContain("Mar");
      expect(result).toContain("16");
    });

    it("returns 'just now' for delta of 0", () => {
      expect(formatTimestamp(NOW, NOW)).toBe("just now");
    });

    it("max width is 19 chars", () => {
      // Absolute format: "Mar 15, 12:00" = 13 chars (well within 19)
      const result = formatTimestamp(NOW - 2 * 86_400_000, NOW);
      expect(result.length).toBeLessThanOrEqual(19);
    });
  });
});
