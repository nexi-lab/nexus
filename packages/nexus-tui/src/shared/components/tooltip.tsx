/**
 * Dismissible first-run tooltip.
 *
 * Shows a brief tip message at the top of a panel on first visit.
 * Dismissed by any key press, tracked via the first-run store so
 * it only shows once per session per key.
 *
 * NOTE: This tooltip is cosmetic-only. It dismisses on any key press but
 * does NOT block parent keyboard handlers. This is intentional -- the
 * tooltip is a hint overlay, not a modal dialog. Parent key bindings
 * (j/k/tab/etc.) continue to work while the tooltip is visible.
 */

import React from "react";
import { useFirstRunStore } from "../../stores/first-run-store.js";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { statusColor } from "../theme.js";
import { textStyle } from "../text-style.js";

interface TooltipProps {
  /** Unique key for this tooltip (e.g. "search-panel", "events-panel"). */
  readonly tooltipKey: string;
  /** The message to display. */
  readonly message: string;
}

export function Tooltip({ tooltipKey, message }: TooltipProps): React.ReactNode {
  const shouldShow = useFirstRunStore((s) => s.shouldShow);
  const dismiss = useFirstRunStore((s) => s.dismiss);

  const visible = shouldShow(tooltipKey);

  // Dismiss on any keypress via onUnhandled only -- no explicit key bindings
  // so we don't intercept j/k/tab/etc. from the parent panel.
  useKeyboard(
    {},
    visible
      ? (keyName: string) => {
          if (keyName) dismiss(tooltipKey);
        }
      : undefined,
  );

  if (!visible) return null;

  return (
    <box height={1} width="100%">
      <text style={textStyle({ fg: statusColor.info })}>
        {`${message}  (press any key to dismiss)`}
      </text>
    </box>
  );
}
