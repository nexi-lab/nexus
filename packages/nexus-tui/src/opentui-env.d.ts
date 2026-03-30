/**
 * Type augmentations for OpenTUI v0.1.87.
 *
 * Fixes ElementClass definition (instance vs constructor mismatch).
 * The original declares `ElementClass extends React.ComponentClass<any>`
 * which incorrectly requires the constructor type rather than the
 * instance type, preventing class components from being used as JSX.
 */
import type React from "react";
import type { RGBA } from "@opentui/core";

declare module "@opentui/react/jsx-runtime" {
  namespace JSX {
    // Fix: ElementClass should describe instances, not constructors.
    interface ElementClass {
      render(): React.ReactNode;
    }
  }
}

declare module "@opentui/core/renderables/Text" {
  interface TextOptions {
    foregroundColor?: string | RGBA;
    backgroundColor?: string | RGBA;
    bold?: boolean;
    dimColor?: boolean;
    inverse?: boolean;
    underline?: boolean;
  }
}

declare module "@opentui/core/renderables/TextNode" {
  interface TextNodeOptions {
    foregroundColor?: string | RGBA;
    backgroundColor?: string | RGBA;
    bold?: boolean;
    dimColor?: boolean;
    inverse?: boolean;
    underline?: boolean;
  }
}

declare module "@opentui/core" {
  interface TextOptions {
    foregroundColor?: string | RGBA;
    backgroundColor?: string | RGBA;
    bold?: boolean;
    dimColor?: boolean;
    inverse?: boolean;
    underline?: boolean;
  }

  interface TextNodeOptions {
    foregroundColor?: string | RGBA;
    backgroundColor?: string | RGBA;
    bold?: boolean;
    dimColor?: boolean;
    inverse?: boolean;
    underline?: boolean;
  }
}
