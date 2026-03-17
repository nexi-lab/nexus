/**
 * Centralized error display bar.
 *
 * Renders the most recent error from the error store above the status bar.
 * Supports dismissal (any key) and retry (r key).
 *
 * @see Issue #3066 Architecture Decision 8A
 */

import React from "react";
import { useErrorStore } from "../../stores/error-store.js";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { statusColor } from "../theme.js";

const CATEGORY_HINTS: Record<string, string> = {
  network: "Check connection. r:retry",
  validation: "Check input values.",
  server: "Server error. r:retry",
};

export function ErrorBar(): React.ReactNode {
  const errors = useErrorStore((s) => s.errors);
  const dismissError = useErrorStore((s) => s.dismissError);

  const latest = errors.length > 0 ? errors[errors.length - 1]! : null;

  useKeyboard(
    latest
      ? {
          r: () => {
            if (latest.retryAction) {
              dismissError(latest.id);
              latest.retryAction();
            }
          },
          escape: () => {
            if (latest.dismissable) dismissError(latest.id);
          },
        }
      : {},
  );

  if (!latest) return null;

  const hint = CATEGORY_HINTS[latest.category] ?? "";
  const prefix = errors.length > 1 ? `(${errors.length}) ` : "";

  return (
    <box height={1} width="100%" flexDirection="row">
      <text>
        <span foregroundColor="#ff4444" bold>{`${prefix}✗ ${latest.message}`}</span>
        <span foregroundColor="#ff8888">{`  ${hint}`}</span>
        {latest.dismissable ? (
          <span foregroundColor="#666666">{"  Esc:dismiss"}</span>
        ) : ""}
      </text>
    </box>
  );
}
