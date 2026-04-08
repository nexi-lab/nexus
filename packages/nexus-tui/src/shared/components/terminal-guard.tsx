/**
 * TerminalGuard — full-screen resize prompt for small terminals (#3501).
 *
 * Shows a friendly "please resize" message when the terminal falls below the
 * minimum usable size (TERMINAL_GUARD_MIN_COLS × TERMINAL_GUARD_MIN_ROWS).
 * Above that threshold the guard is transparent — children render normally.
 *
 * Design decisions:
 * - Guard fires at 60×24 (not 80×24) so the hidden-sidebar layout mode
 *   (60–79 cols) is reachable before the guard blocks the UI.
 * - Non-TTY output (piped, CI): tooSmall() is always false, guard never fires.
 * - Follows the pre-connection screen pattern in app.tsx: two complementary
 *   <Show> blocks so App()'s reactive scope (SSE, effects) is never affected.
 */

import { Show } from "solid-js";
import type { JSX } from "solid-js";
import { terminalDimensions, isTooSmall } from "../terminal-dimensions.js";
import {
  TERMINAL_GUARD_MIN_COLS,
  TERMINAL_GUARD_MIN_ROWS,
} from "./side-nav-utils.js";
import { statusColor } from "../theme.js";
import { textStyle } from "../text-style.js";

interface TerminalGuardProps {
  readonly children: JSX.Element;
}

export function TerminalGuard(props: TerminalGuardProps): JSX.Element {
  // Shared threshold check — single source of truth in terminal-dimensions.ts.
  // Non-TTY (piped output, CI): always false, guard never fires.
  const tooSmall = isTooSmall;

  return (
    <box height="100%" width="100%" flexDirection="column">
      <Show when={!tooSmall()}>{props.children}</Show>
      <Show when={tooSmall()}>
        <box height="100%" width="100%" justifyContent="center" alignItems="center" flexDirection="column">
          <text style={textStyle({ bold: true, fg: statusColor.warning })}>
            {"Terminal too small"}
          </text>
          <text>
            {`Resize to at least ${TERMINAL_GUARD_MIN_COLS}×${TERMINAL_GUARD_MIN_ROWS} to use Nexus TUI.`}
          </text>
          <text style={textStyle({ dim: true })}>
            {`(current: ${terminalDimensions().width}×${terminalDimensions().height})`}
          </text>
        </box>
      </Show>
    </box>
  );
}
