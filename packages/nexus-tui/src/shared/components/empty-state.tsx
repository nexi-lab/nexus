/**
 * Consistent empty state component for panels with no data.
 *
 * Replaces generic "No X found" with actionable messages that tell
 * users what to do next.
 *
 * @see Issue #3066, Phase E10
 */

import { Show } from "solid-js";
import { statusColor } from "../theme.js";
import { textStyle } from "../text-style.js";

interface EmptyStateProps {
  /** Primary message, e.g. "No transactions yet." */
  readonly message: string;
  /** Optional hint showing what to do, e.g. "Press n to begin one." */
  readonly hint?: string;
}

export function EmptyState(props: EmptyStateProps) {
  return (
    <box
      height="100%"
      width="100%"
      justifyContent="center"
      alignItems="center"
      flexDirection="column"
    >
      <text style={textStyle({ dim: true })}>{props.message}</text>
      <Show when={props.hint}>
        <text style={textStyle({ fg: statusColor.dim })}>{props.hint}</text>
      </Show>
    </box>
  );
}
