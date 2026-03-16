/**
 * Render ANSI-styled text in OpenTUI.
 *
 * Takes raw text containing ANSI escape codes and renders it as
 * a series of styled <text> elements using the `anser` library
 * (6M+ weekly downloads, used by Jest/Jupyter).
 *
 * @see Issue #3066 Architecture Decision 1C
 */

import React from "react";
import Anser from "anser";

interface StyledTextProps {
  /** Raw text potentially containing ANSI escape codes. */
  readonly children: string;
}

export function StyledText({ children }: StyledTextProps): React.ReactNode {
  if (!children) return null;

  const spans = Anser.ansiToJson(children, {
    json: true,
    remove_empty: true,
  });

  if (spans.length === 0) return null;

  // Single unstyled span — render directly
  if (spans.length === 1 && !spans[0]!.was_processed) {
    return <text>{spans[0]!.content}</text>;
  }

  return (
    <text>
      {spans.map((span, i) => {
        const decoration = span.decoration ?? "";
        return (
          <text
            key={i}
            bold={decoration.includes("bold") || undefined}
            dimColor={decoration.includes("dim") || undefined}
            underline={decoration.includes("underline") || undefined}
            inverse={decoration.includes("reverse") || undefined}
            foregroundColor={span.fg ? `rgb(${span.fg})` : undefined}
            backgroundColor={span.bg ? `rgb(${span.bg})` : undefined}
          >
            {span.content}
          </text>
        );
      })}
    </text>
  );
}

/**
 * Strip all ANSI escape sequences from a string, returning plain text.
 * Re-exported from anser for convenience.
 */
export function stripAnsi(input: string): string {
  return Anser.ansiToText(input);
}
