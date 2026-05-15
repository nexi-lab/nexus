/**
 * Terminal lifecycle utilities.
 *
 * Centralises the escape sequences and stdin teardown needed to restore a
 * clean terminal state after TUI exit or crash.  Both the normal shutdown
 * path (app.tsx) and the fatal-error catch (index.tsx) use resetTerminal()
 * so the sequences stay in sync.
 */

import fs from "fs";

/**
 * Escape sequences written by resetTerminal().
 * Exported so tests can assert all five are present without re-specifying them.
 */
export const TERMINAL_RESET_SEQUENCES = [
  "\x1b[?1003l", // disable all-motion mouse tracking
  "\x1b[?1006l", // disable SGR mouse mode
  "\x1b[?1000l", // disable normal mouse tracking
  "\x1b[?1049l", // switch back to main screen
  "\x1b[?25h",   // show cursor
] as const;

/**
 * Restores terminal state after TUI exit or crash.
 *
 * Stops raw-mode stdin, then writes the reset sequences synchronously so
 * they are guaranteed to flush before process.exit().  The sequences are
 * no-ops when the terminal was never reconfigured, so it is always safe to
 * call this function.
 */
export function resetTerminal(): void {
  if (process.stdin.setRawMode) {
    process.stdin.setRawMode(false);
  }
  process.stdin.pause();
  fs.writeSync(1, TERMINAL_RESET_SEQUENCES.join(""));
}
