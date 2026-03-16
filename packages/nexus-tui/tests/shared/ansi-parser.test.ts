import { describe, it, expect } from "bun:test";
import { parseAnsi, stripAnsi, type StyledSpan } from "../../src/shared/lib/ansi-parser.js";

// Helper: build an ANSI SGR sequence
const sgr = (...codes: number[]) => `\x1b[${codes.join(";")}m`;

describe("parseAnsi", () => {
  describe("plain text", () => {
    it("returns single span for text without escapes", () => {
      const spans = parseAnsi("hello world");
      expect(spans).toEqual([{ text: "hello world", style: {} }]);
    });

    it("returns empty array for empty string", () => {
      expect(parseAnsi("")).toEqual([]);
    });
  });

  describe("basic SGR attributes", () => {
    it("parses bold", () => {
      const spans = parseAnsi(`${sgr(1)}bold${sgr(0)}`);
      expect(spans[0]!.style.bold).toBe(true);
      expect(spans[0]!.text).toBe("bold");
    });

    it("parses dim", () => {
      const spans = parseAnsi(`${sgr(2)}dim${sgr(0)}`);
      expect(spans[0]!.style.dim).toBe(true);
    });

    it("parses italic", () => {
      const spans = parseAnsi(`${sgr(3)}italic${sgr(0)}`);
      expect(spans[0]!.style.italic).toBe(true);
    });

    it("parses underline", () => {
      const spans = parseAnsi(`${sgr(4)}underline${sgr(0)}`);
      expect(spans[0]!.style.underline).toBe(true);
    });

    it("parses inverse", () => {
      const spans = parseAnsi(`${sgr(7)}inverse${sgr(0)}`);
      expect(spans[0]!.style.inverse).toBe(true);
    });

    it("parses strikethrough", () => {
      const spans = parseAnsi(`${sgr(9)}strike${sgr(0)}`);
      expect(spans[0]!.style.strikethrough).toBe(true);
    });

    it("resets all attributes with SGR 0", () => {
      const spans = parseAnsi(`${sgr(1, 3, 4)}styled${sgr(0)}plain`);
      expect(spans[0]!.style.bold).toBe(true);
      expect(spans[0]!.style.italic).toBe(true);
      expect(spans[1]!.style).toEqual({});
      expect(spans[1]!.text).toBe("plain");
    });

    it("handles empty params as reset", () => {
      const spans = parseAnsi(`${sgr(1)}bold\x1b[mplain`);
      expect(spans[0]!.style.bold).toBe(true);
      expect(spans[1]!.style).toEqual({});
    });
  });

  describe("standard colors (30-37, 40-47)", () => {
    it("parses foreground colors", () => {
      const spans = parseAnsi(`${sgr(31)}red${sgr(32)}green${sgr(0)}`);
      expect(spans[0]!.style.fg).toBe("red");
      expect(spans[0]!.text).toBe("red");
      expect(spans[1]!.style.fg).toBe("green");
      expect(spans[1]!.text).toBe("green");
    });

    it("parses background colors", () => {
      const spans = parseAnsi(`${sgr(44)}blue bg${sgr(0)}`);
      expect(spans[0]!.style.bg).toBe("blue");
    });

    it("resets fg with 39", () => {
      const spans = parseAnsi(`${sgr(31)}red${sgr(39)}default`);
      expect(spans[1]!.style.fg).toBeUndefined();
    });

    it("resets bg with 49", () => {
      const spans = parseAnsi(`${sgr(41)}red bg${sgr(49)}default`);
      expect(spans[1]!.style.bg).toBeUndefined();
    });
  });

  describe("bright colors (90-97, 100-107)", () => {
    it("parses bright foreground", () => {
      const spans = parseAnsi(`${sgr(91)}bright red${sgr(0)}`);
      expect(spans[0]!.style.fg).toBe("redBright");
    });

    it("parses bright background", () => {
      const spans = parseAnsi(`${sgr(104)}bright blue bg${sgr(0)}`);
      expect(spans[0]!.style.bg).toBe("blueBright");
    });
  });

  describe("256-color (38;5;N, 48;5;N)", () => {
    it("parses 256-color foreground (standard range)", () => {
      const spans = parseAnsi(`${sgr(38, 5, 1)}red${sgr(0)}`);
      expect(spans[0]!.style.fg).toBe("red");
    });

    it("parses 256-color foreground (bright range)", () => {
      const spans = parseAnsi(`${sgr(38, 5, 9)}bright red${sgr(0)}`);
      expect(spans[0]!.style.fg).toBe("redBright");
    });

    it("parses 256-color foreground (cube range)", () => {
      const spans = parseAnsi(`${sgr(38, 5, 196)}color${sgr(0)}`);
      // 196 = 16 + (180) → R=5*51=255, G=0, B=0
      expect(spans[0]!.style.fg).toBe("#ff0000");
    });

    it("parses 256-color foreground (grayscale)", () => {
      const spans = parseAnsi(`${sgr(38, 5, 240)}gray${sgr(0)}`);
      // 240 = 232 + 8 → gray = 8*10+8 = 88
      expect(spans[0]!.style.fg).toBe("#585858");
    });

    it("parses 256-color background", () => {
      const spans = parseAnsi(`${sgr(48, 5, 21)}blue${sgr(0)}`);
      expect(spans[0]!.style.bg).toBeDefined();
    });
  });

  describe("true-color (38;2;R;G;B, 48;2;R;G;B)", () => {
    it("parses true-color foreground", () => {
      const spans = parseAnsi(`${sgr(38, 2, 255, 128, 0)}orange${sgr(0)}`);
      expect(spans[0]!.style.fg).toBe("#ff8000");
    });

    it("parses true-color background", () => {
      const spans = parseAnsi(`${sgr(48, 2, 0, 0, 128)}navy bg${sgr(0)}`);
      expect(spans[0]!.style.bg).toBe("#000080");
    });
  });

  describe("combined styles", () => {
    it("handles bold + color in single sequence", () => {
      const spans = parseAnsi(`${sgr(1, 31)}bold red${sgr(0)}`);
      expect(spans[0]!.style.bold).toBe(true);
      expect(spans[0]!.style.fg).toBe("red");
    });

    it("accumulates styles across sequences", () => {
      const spans = parseAnsi(`${sgr(1)}${sgr(31)}bold red${sgr(0)}`);
      expect(spans[0]!.style.bold).toBe(true);
      expect(spans[0]!.style.fg).toBe("red");
    });
  });

  describe("non-SGR CSI sequences", () => {
    it("strips cursor movement sequences", () => {
      const spans = parseAnsi("hello\x1b[2Aworld");
      expect(spans).toHaveLength(2);
      expect(spans[0]!.text).toBe("hello");
      expect(spans[1]!.text).toBe("world");
    });

    it("strips erase sequences", () => {
      const spans = parseAnsi("hello\x1b[2Jworld");
      const text = spans.map((s) => s.text).join("");
      expect(text).toBe("helloworld");
    });
  });

  describe("OSC sequences", () => {
    it("strips OSC title sequences", () => {
      const spans = parseAnsi("hello\x1b]0;title\x07world");
      const text = spans.map((s) => s.text).join("");
      expect(text).toBe("helloworld");
    });
  });

  describe("multiline", () => {
    it("preserves newlines as text", () => {
      const spans = parseAnsi(`${sgr(31)}line1\nline2${sgr(0)}`);
      expect(spans[0]!.text).toBe("line1\nline2");
    });
  });
});

describe("stripAnsi", () => {
  it("removes all ANSI sequences", () => {
    expect(stripAnsi(`${sgr(1, 31)}hello${sgr(0)} world`)).toBe("hello world");
  });

  it("returns plain text unchanged", () => {
    expect(stripAnsi("hello world")).toBe("hello world");
  });

  it("handles empty string", () => {
    expect(stripAnsi("")).toBe("");
  });

  it("strips cursor movement", () => {
    expect(stripAnsi("abc\x1b[2Adef")).toBe("abcdef");
  });
});
