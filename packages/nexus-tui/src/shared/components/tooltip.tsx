/**
 * Dismissible first-run tooltip.
 *
 * Shows a brief tip message at the top of a panel on first visit.
 * Dismissed by any key press, tracked via the first-run store so
 * it only shows once per session per key.
 */

import React, { useEffect } from "react";
import { useFirstRunStore } from "../../stores/first-run-store.js";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { statusColor } from "../theme.js";

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

  useKeyboard(
    visible
      ? {
          // Any of these common keys dismisses the tooltip
          return: () => dismiss(tooltipKey),
          escape: () => dismiss(tooltipKey),
          tab: () => dismiss(tooltipKey),
          j: () => dismiss(tooltipKey),
          k: () => dismiss(tooltipKey),
          "/": () => dismiss(tooltipKey),
          "?": () => dismiss(tooltipKey),
        }
      : {},
    visible
      ? (keyName: string) => {
          // Any unhandled key also dismisses
          if (keyName) dismiss(tooltipKey);
        }
      : undefined,
  );

  if (!visible) return null;

  return (
    <box height={1} width="100%">
      <text foregroundColor={statusColor.info}>
        {`${message}  (press any key to dismiss)`}
      </text>
    </box>
  );
}
