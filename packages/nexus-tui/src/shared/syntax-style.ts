import { SyntaxStyle } from "@opentui/core";
import { palette } from "./theme.js";

/**
 * Nexus syntax highlight theme.
 *
 * Keys are Tree-sitter scope names. Values map to the Nexus palette so code
 * blocks share the same visual language as the rest of the TUI chrome.
 *
 * Scopes follow the dot-hierarchy: "keyword.control" inherits from "keyword"
 * if no explicit entry exists for the more specific name.
 */
export const defaultSyntaxStyle = SyntaxStyle.fromStyles({
  // Keywords
  "keyword":                  { fg: palette.accent },
  "keyword.control":          { fg: palette.accent, bold: true },
  "keyword.operator":         { fg: palette.accent },

  // Types
  "type":                     { fg: palette.warning },
  "type.builtin":             { fg: palette.warning },

  // Functions
  "function":                 { fg: "#7DD3FC" },  // sky-300 — distinct from accent
  "function.builtin":         { fg: "#7DD3FC", bold: true },
  "function.method":          { fg: "#7DD3FC" },

  // Variables / properties
  "variable":                 { fg: palette.bright },
  "variable.builtin":         { fg: palette.muted, italic: true },
  "property":                 { fg: "#A5B4FC" },  // indigo-300

  // Strings
  "string":                   { fg: palette.success },
  "string.escape":            { fg: palette.warning },
  "string.special":           { fg: palette.success, bold: true },

  // Constants & numbers
  "constant":                 { fg: palette.warning },
  "constant.builtin":         { fg: palette.warning, bold: true },
  "number":                   { fg: palette.warning },
  "boolean":                  { fg: palette.warning, bold: true },

  // Comments
  "comment":                  { fg: palette.muted, italic: true },
  "comment.documentation":    { fg: palette.muted },

  // Operators & punctuation
  "operator":                 { fg: palette.bright },
  "punctuation":              { fg: palette.muted },
  "punctuation.delimiter":    { fg: palette.muted },
  "punctuation.bracket":      { fg: palette.bright },

  // Markup / HTML / JSX
  "tag":                      { fg: palette.error },
  "attribute":                { fg: palette.warning },

  // Errors
  "error":                    { fg: palette.error, bold: true },
});
