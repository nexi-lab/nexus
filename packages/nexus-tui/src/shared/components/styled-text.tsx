/**
 * Render ANSI-styled text in OpenTUI.
 *
 * Takes raw text containing ANSI escape codes and renders it as
 * a series of styled <text> elements using the ANSI parser.
 *
 * @see Issue #3066 Architecture Decision 1C
 */

import React from "react";
import { parseAnsi, type StyledSpan } from "../lib/ansi-parser.js";

interface StyledTextProps {
  /** Raw text potentially containing ANSI escape codes. */
  readonly children: string;
}

export function StyledText({ children }: StyledTextProps): React.ReactNode {
  const spans = parseAnsi(children);

  if (spans.length === 0) return null;

  // Single unstyled span — render directly
  if (spans.length === 1 && Object.keys(spans[0]!.style).length === 0) {
    return <text>{spans[0]!.text}</text>;
  }

  return (
    <text>
      {spans.map((span, i) => (
        <text
          key={i}
          bold={span.style.bold}
          dimColor={span.style.dim}
          underline={span.style.underline}
          inverse={span.style.inverse}
          foregroundColor={span.style.fg}
          backgroundColor={span.style.bg}
        >
          {span.text}
        </text>
      ))}
    </text>
  );
}
