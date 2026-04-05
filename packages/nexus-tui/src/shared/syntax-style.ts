import { SyntaxStyle } from "@opentui/core";
import { palette } from "./theme.js";

/**
 * Nexus syntax highlight theme.
 *
 * Uses SyntaxStyle.fromTheme() so that hex color strings are parsed via
 * parseColor() into RGBA — fromStyles() expects RGBA objects, not strings.
 *
 * Scopes follow the dot-hierarchy: "keyword.control" inherits from "keyword"
 * if no explicit entry exists for the more specific name.
 */
export const defaultSyntaxStyle = SyntaxStyle.fromTheme([
  // Keywords
  { scope: ["keyword"],             style: { foreground: palette.accent } },
  { scope: ["keyword.control"],     style: { foreground: palette.accent, bold: true } },
  { scope: ["keyword.operator"],    style: { foreground: palette.accent } },

  // Types
  { scope: ["type"],                style: { foreground: palette.warning } },
  { scope: ["type.builtin"],        style: { foreground: palette.warning } },

  // Functions
  { scope: ["function"],            style: { foreground: "#7DD3FC" } },  // sky-300
  { scope: ["function.builtin"],    style: { foreground: "#7DD3FC", bold: true } },
  { scope: ["function.method"],     style: { foreground: "#7DD3FC" } },

  // Variables / properties
  { scope: ["variable"],            style: { foreground: palette.bright } },
  { scope: ["variable.builtin"],    style: { foreground: palette.muted, italic: true } },
  { scope: ["property"],            style: { foreground: "#A5B4FC" } },  // indigo-300

  // Strings
  { scope: ["string"],              style: { foreground: palette.success } },
  { scope: ["string.escape"],       style: { foreground: palette.warning } },
  { scope: ["string.special"],      style: { foreground: palette.success, bold: true } },

  // Constants & numbers
  { scope: ["constant"],            style: { foreground: palette.warning } },
  { scope: ["constant.builtin"],    style: { foreground: palette.warning, bold: true } },
  { scope: ["number"],              style: { foreground: palette.warning } },
  { scope: ["boolean"],             style: { foreground: palette.warning, bold: true } },

  // Comments
  { scope: ["comment"],             style: { foreground: palette.muted, italic: true } },
  { scope: ["comment.documentation"], style: { foreground: palette.muted } },

  // Operators & punctuation
  { scope: ["operator"],            style: { foreground: palette.bright } },
  { scope: ["punctuation"],         style: { foreground: palette.muted } },
  { scope: ["punctuation.delimiter"], style: { foreground: palette.muted } },
  { scope: ["punctuation.bracket"], style: { foreground: palette.bright } },

  // Markup / HTML / JSX
  { scope: ["tag"],                 style: { foreground: palette.error } },
  { scope: ["attribute"],           style: { foreground: palette.warning } },

  // Errors
  { scope: ["error"],               style: { foreground: palette.error, bold: true } },
]);
