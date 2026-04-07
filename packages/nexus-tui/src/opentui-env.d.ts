/**
 * Local type augmentations for OpenTUI Solid bindings.
 */
import type { RGBA } from "@opentui/core";
import type { TextNodeOptions, TextOptions } from "@opentui/core";
import type { SpanProps, TextProps } from "@opentui/solid/src/types/elements.js";

declare module "@opentui/solid/jsx-runtime" {
  namespace JSX {
    interface IntrinsicAttributes {
      key?: string | number;
    }

    interface IntrinsicElements {
      span: SpanProps & TextNodeOptions;
      b: SpanProps & TextNodeOptions;
      strong: SpanProps & TextNodeOptions;
      i: SpanProps & TextNodeOptions;
      em: SpanProps & TextNodeOptions;
      u: SpanProps & TextNodeOptions;
      text: TextProps & TextOptions;
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
