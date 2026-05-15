import { createTextAttributes } from "@opentui/core";

interface TextStyleOptions {
  readonly fg?: string;
  readonly bg?: string;
  readonly bold?: boolean;
  readonly dim?: boolean;
  readonly underline?: boolean;
  readonly inverse?: boolean;
}

export function textStyle(options: TextStyleOptions = {}): {
  fg?: string;
  bg?: string;
  attributes: number;
} {
  const { fg, bg, bold, dim, underline, inverse } = options;
  return {
    fg,
    bg,
    attributes: createTextAttributes({ bold, dim, underline, inverse }),
  };
}
