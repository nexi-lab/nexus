/**
 * Shared loading indicator with context message.
 *
 * Replaces all inline "loading..." text across panels with a consistent
 * component that includes the existing Spinner animation.
 *
 * @see Issue #3066, Phase E5
 */

import React from "react";
import { Spinner } from "./spinner.js";

interface LoadingIndicatorProps {
  /** Context message shown next to the spinner. Default: "Loading..." */
  readonly message?: string;
  /** Whether to center within parent. Default: true */
  readonly centered?: boolean;
}

export function LoadingIndicator({
  message = "Loading...",
  centered = true,
}: LoadingIndicatorProps): React.ReactNode {
  const content = <Spinner label={message} />;

  if (centered) {
    return (
      <box
        height="100%"
        width="100%"
        justifyContent="center"
        alignItems="center"
      >
        {content}
      </box>
    );
  }

  return content;
}
