/**
 * Lightweight ANSI SGR escape code parser.
 *
 * Parses ANSI-colored terminal output into styled spans for rendering
 * in OpenTUI <text> elements. Handles SGR (Select Graphic Rendition)
 * codes — the subset of ANSI escapes used for text styling.
 *
 * Covers: bold, dim, italic, underline, strikethrough, inverse,
 * 8 standard colors, 8 bright colors, 256-color, and true-color (24-bit).
 *
 * Does NOT handle cursor movement, screen clearing, or other VT100
 * control sequences — those are stripped silently.
 *
 * @see Issue #3066 Architecture Decision 1C
 */

// =============================================================================
// Types
// =============================================================================

export interface AnsiStyle {
  readonly bold?: boolean;
  readonly dim?: boolean;
  readonly italic?: boolean;
  readonly underline?: boolean;
  readonly strikethrough?: boolean;
  readonly inverse?: boolean;
  readonly fg?: string;
  readonly bg?: string;
}

export interface StyledSpan {
  readonly text: string;
  readonly style: AnsiStyle;
}

// =============================================================================
// ANSI color tables
// =============================================================================

const STANDARD_COLORS: readonly string[] = [
  "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
];

const BRIGHT_COLORS: readonly string[] = [
  "blackBright", "redBright", "greenBright", "yellowBright",
  "blueBright", "magentaBright", "cyanBright", "whiteBright",
];

// 256-color palette: 0-7 = standard, 8-15 = bright, 16-231 = 6x6x6 cube, 232-255 = grayscale
function color256(n: number): string {
  if (n >= 0 && n < 8) return STANDARD_COLORS[n]!;
  if (n >= 8 && n < 16) return BRIGHT_COLORS[n - 8]!;
  if (n >= 16 && n < 232) {
    // 6x6x6 color cube
    const idx = n - 16;
    const r = Math.floor(idx / 36) * 51;
    const g = Math.floor((idx % 36) / 6) * 51;
    const b = (idx % 6) * 51;
    return `#${hex(r)}${hex(g)}${hex(b)}`;
  }
  // Grayscale: 232-255 → 8, 18, ..., 238
  const gray = (n - 232) * 10 + 8;
  return `#${hex(gray)}${hex(gray)}${hex(gray)}`;
}

function hex(n: number): string {
  return Math.min(255, Math.max(0, n)).toString(16).padStart(2, "0");
}

// =============================================================================
// CSI sequence regex
// =============================================================================

// Matches CSI (Control Sequence Introducer) sequences: ESC [ ... final_byte
// Also matches OSC (ESC ]) and other escape sequences for stripping
const CSI_REGEX = /\x1b\[([0-9;]*)([A-Za-z])/g;
const OTHER_ESCAPE_REGEX = /\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)?|[^[].?)/g;

// =============================================================================
// Parser
// =============================================================================

/**
 * Parse a string containing ANSI escape codes into styled spans.
 *
 * Non-SGR CSI sequences (cursor movement, etc.) are silently stripped.
 * Bare text without escapes is returned as a single unstyled span.
 */
export function parseAnsi(input: string): StyledSpan[] {
  if (!input.includes("\x1b")) {
    return input ? [{ text: input, style: {} }] : [];
  }

  const spans: StyledSpan[] = [];
  let style: AnsiStyle = {};
  let lastIndex = 0;

  // Strip non-CSI escape sequences first (OSC, etc.)
  const cleaned = input.replace(OTHER_ESCAPE_REGEX, "");

  // Reset regex state
  CSI_REGEX.lastIndex = 0;

  let match: RegExpExecArray | null;
  while ((match = CSI_REGEX.exec(cleaned)) !== null) {
    // Text before this escape sequence
    if (match.index > lastIndex) {
      const text = cleaned.slice(lastIndex, match.index);
      if (text) spans.push({ text, style });
    }

    lastIndex = match.index + match[0].length;
    const finalByte = match[2]!;

    // Only process SGR (final byte 'm')
    if (finalByte === "m") {
      style = applySgr(style, match[1]!);
    }
    // All other CSI sequences are silently dropped
  }

  // Remaining text after last escape
  if (lastIndex < cleaned.length) {
    const text = cleaned.slice(lastIndex);
    if (text) spans.push({ text, style });
  }

  return spans;
}

// =============================================================================
// SGR application
// =============================================================================

function applySgr(current: AnsiStyle, params: string): AnsiStyle {
  const codes = params === "" ? [0] : params.split(";").map(Number);
  let style = { ...current };

  for (let i = 0; i < codes.length; i++) {
    const code = codes[i]!;

    switch (code) {
      case 0: // Reset
        style = {};
        break;
      case 1:
        style = { ...style, bold: true };
        break;
      case 2:
        style = { ...style, dim: true };
        break;
      case 3:
        style = { ...style, italic: true };
        break;
      case 4:
        style = { ...style, underline: true };
        break;
      case 7:
        style = { ...style, inverse: true };
        break;
      case 9:
        style = { ...style, strikethrough: true };
        break;
      case 22: // Normal intensity (not bold, not dim)
        style = { ...style, bold: undefined, dim: undefined };
        break;
      case 23:
        style = { ...style, italic: undefined };
        break;
      case 24:
        style = { ...style, underline: undefined };
        break;
      case 27:
        style = { ...style, inverse: undefined };
        break;
      case 29:
        style = { ...style, strikethrough: undefined };
        break;

      // Standard foreground colors (30-37)
      case 30: case 31: case 32: case 33:
      case 34: case 35: case 36: case 37:
        style = { ...style, fg: STANDARD_COLORS[code - 30]! };
        break;

      // Default foreground
      case 39:
        style = { ...style, fg: undefined };
        break;

      // Standard background colors (40-47)
      case 40: case 41: case 42: case 43:
      case 44: case 45: case 46: case 47:
        style = { ...style, bg: STANDARD_COLORS[code - 40]! };
        break;

      // Default background
      case 49:
        style = { ...style, bg: undefined };
        break;

      // Bright foreground colors (90-97)
      case 90: case 91: case 92: case 93:
      case 94: case 95: case 96: case 97:
        style = { ...style, fg: BRIGHT_COLORS[code - 90]! };
        break;

      // Bright background colors (100-107)
      case 100: case 101: case 102: case 103:
      case 104: case 105: case 106: case 107:
        style = { ...style, bg: BRIGHT_COLORS[code - 100]! };
        break;

      // Extended color: 38;5;N (256-color fg) or 38;2;R;G;B (true-color fg)
      case 38: {
        const mode = codes[i + 1];
        if (mode === 5 && i + 2 < codes.length) {
          style = { ...style, fg: color256(codes[i + 2]!) };
          i += 2;
        } else if (mode === 2 && i + 4 < codes.length) {
          const r = codes[i + 2]!;
          const g = codes[i + 3]!;
          const b = codes[i + 4]!;
          style = { ...style, fg: `#${hex(r)}${hex(g)}${hex(b)}` };
          i += 4;
        }
        break;
      }

      // Extended color: 48;5;N (256-color bg) or 48;2;R;G;B (true-color bg)
      case 48: {
        const mode = codes[i + 1];
        if (mode === 5 && i + 2 < codes.length) {
          style = { ...style, bg: color256(codes[i + 2]!) };
          i += 2;
        } else if (mode === 2 && i + 4 < codes.length) {
          const r = codes[i + 2]!;
          const g = codes[i + 3]!;
          const b = codes[i + 4]!;
          style = { ...style, bg: `#${hex(r)}${hex(g)}${hex(b)}` };
          i += 4;
        }
        break;
      }
    }
  }

  return style;
}

// =============================================================================
// Utility: strip all ANSI codes from a string
// =============================================================================

/**
 * Remove all ANSI escape sequences, returning plain text.
 */
export function stripAnsi(input: string): string {
  return input
    .replace(CSI_REGEX, "")
    .replace(OTHER_ESCAPE_REGEX, "");
}
