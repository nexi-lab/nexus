/**
 * Centralized error display bar.
 *
 * Renders the most recent error from the error store above the status bar.
 * Supports dismissal (any key) and retry (r key).
 *
 * @see Issue #3066 Architecture Decision 8A
 */

import { useErrorStore } from "../../stores/error-store.js";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { palette } from "../theme.js";
import { textStyle } from "../text-style.js";

const CATEGORY_HINTS: Record<string, string> = {
  network: "Check connection. r:retry",
  validation: "Check input values.",
  server: "Server error. r:retry",
};

export function ErrorBar() {
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
        <span style={textStyle({ fg: palette.error, bold: true })}>{`${prefix}✗ ${latest.message}`}</span>
        <span style={textStyle({ fg: palette.errorDim })}>{`  ${hint}`}</span>
        {latest.dismissable ? (
          <span style={textStyle({ fg: palette.faint })}>{"  Esc:dismiss"}</span>
        ) : ""}
      </text>
    </box>
  );
}
