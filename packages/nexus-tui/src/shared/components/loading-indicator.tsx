/**
 * Shared loading indicator with context message.
 *
 * Replaces all inline "loading..." text across panels with a consistent
 * component that includes the existing Spinner animation.
 *
 * @see Issue #3066, Phase E5
 */

import { Spinner } from "./spinner.js";

interface LoadingIndicatorProps {
  /** Context message shown next to the spinner. Default: "Loading..." */
  readonly message?: string;
  /** Whether to center within parent. Default: true */
  readonly centered?: boolean;
}

export function LoadingIndicator(props: LoadingIndicatorProps) {
  const content = <Spinner label={props.message ?? "Loading..."} />;

  if (props.centered ?? true) {
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
