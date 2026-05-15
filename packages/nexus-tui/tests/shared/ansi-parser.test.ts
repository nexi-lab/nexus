/**
 * Tests for ANSI rendering via anser library integration.
 *
 * Tests the anser library's ansiToJson output format that StyledText
 * depends on, plus our stripAnsi re-export.
 */

import { describe, it, expect } from "bun:test";
import Anser from "anser";
import { stripAnsi } from "../../src/shared/components/styled-text.js";

// Helper: build an ANSI SGR sequence
const sgr = (...codes: number[]) => `\x1b[${codes.join(";")}m`;

describe("anser integration", () => {
  describe("ansiToJson — span format", () => {
    it("returns spans with content property", () => {
      const spans = Anser.ansiToJson(`${sgr(31)}red${sgr(0)}`, { json: true });
      expect(spans.length).toBeGreaterThanOrEqual(1);
      const redSpan = spans.find((s) => s.content === "red");
      expect(redSpan).toBeDefined();
    });

    it("sets fg color for standard colors", () => {
      const spans = Anser.ansiToJson(`${sgr(31)}red text${sgr(0)}`, { json: true });
      const redSpan = spans.find((s) => s.content === "red text");
      expect(redSpan).toBeDefined();
      expect(redSpan!.fg).toBeTruthy();
    });

    it("sets bg color for background codes", () => {
      const spans = Anser.ansiToJson(`${sgr(44)}blue bg${sgr(0)}`, { json: true });
      const span = spans.find((s) => s.content === "blue bg");
      expect(span).toBeDefined();
      expect(span!.bg).toBeTruthy();
    });

    it("sets bold decoration", () => {
      const spans = Anser.ansiToJson(`${sgr(1)}bold${sgr(0)}`, { json: true });
      const span = spans.find((s) => s.content === "bold");
      expect(span).toBeDefined();
      expect(span!.decoration).toContain("bold");
    });

    it("sets dim decoration", () => {
      const spans = Anser.ansiToJson(`${sgr(2)}dim${sgr(0)}`, { json: true });
      const span = spans.find((s) => s.content === "dim");
      expect(span).toBeDefined();
      expect(span!.decoration).toContain("dim");
    });

    it("sets underline decoration", () => {
      const spans = Anser.ansiToJson(`${sgr(4)}underline${sgr(0)}`, { json: true });
      const span = spans.find((s) => s.content === "underline");
      expect(span).toBeDefined();
      expect(span!.decoration).toContain("underline");
    });

    it("handles combined styles", () => {
      const spans = Anser.ansiToJson(`${sgr(1, 31)}bold red${sgr(0)}`, { json: true });
      const span = spans.find((s) => s.content === "bold red");
      expect(span).toBeDefined();
      expect(span!.fg).toBeTruthy();
      expect(span!.decoration).toContain("bold");
    });

    it("handles reset code", () => {
      const spans = Anser.ansiToJson(`${sgr(1)}bold${sgr(0)}plain`, { json: true });
      const plainSpan = spans.find((s) => s.content === "plain");
      expect(plainSpan).toBeDefined();
      // After reset, decoration should be null/empty or not contain bold
      const deco = plainSpan!.decoration ?? "";
      expect(deco.includes("bold")).toBe(false);
    });

    it("returns plain text without ANSI as single span", () => {
      const spans = Anser.ansiToJson("hello world", { json: true });
      expect(spans).toHaveLength(1);
      expect(spans[0]!.content).toBe("hello world");
    });

    it("handles 256-color codes", () => {
      const spans = Anser.ansiToJson(`${sgr(38, 5, 196)}color${sgr(0)}`, { json: true });
      const span = spans.find((s) => s.content === "color");
      expect(span).toBeDefined();
      expect(span!.fg).toBeTruthy();
    });

    it("handles true-color (24-bit) codes", () => {
      const spans = Anser.ansiToJson(`${sgr(38, 2, 255, 128, 0)}orange${sgr(0)}`, { json: true });
      const span = spans.find((s) => s.content === "orange");
      expect(span).toBeDefined();
      expect(span!.fg).toBeTruthy();
    });
  });

  describe("stripAnsi (re-exported from anser)", () => {
    it("removes all ANSI sequences", () => {
      expect(stripAnsi(`${sgr(1, 31)}hello${sgr(0)} world`)).toBe("hello world");
    });

    it("returns plain text unchanged", () => {
      expect(stripAnsi("hello world")).toBe("hello world");
    });

    it("handles empty string", () => {
      expect(stripAnsi("")).toBe("");
    });

    it("strips multiple sequences", () => {
      expect(stripAnsi(`${sgr(31)}red${sgr(32)}green${sgr(0)}plain`)).toBe("redgreenplain");
    });
  });

  describe("remove_empty option", () => {
    it("removes empty spans when enabled", () => {
      const spans = Anser.ansiToJson(`${sgr(31)}${sgr(32)}text`, {
        json: true,
        remove_empty: true,
      });
      const emptySpans = spans.filter((s) => s.content === "");
      expect(emptySpans).toHaveLength(0);
    });
  });
});
