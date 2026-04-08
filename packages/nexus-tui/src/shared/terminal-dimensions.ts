/**
 * Centralized terminal dimension signal (#3501).
 *
 * One module-level source of truth for terminal width/height.
 * All components read this signal instead of installing their own
 * process.stdout resize listeners.
 *
 * - Single resize listener for the entire process (not per-component)
 * - RESIZE_DEBOUNCE_MS (150 ms) debounce to avoid excessive re-renders
 * - Explicit isTTY guard: no listener registered for piped/non-TTY output
 */

import { createSignal } from "solid-js";
import {
  RESIZE_DEBOUNCE_MS,
  TERMINAL_GUARD_MIN_COLS,
  TERMINAL_GUARD_MIN_ROWS,
} from "./components/side-nav-utils.js";

export interface TerminalDimensions {
  readonly width: number;
  readonly height: number;
}

/** Read current terminal size from process.stdout, with safe defaults for non-TTY. */
export function readDimensions(): TerminalDimensions {
  return {
    width: process.stdout.columns ?? TERMINAL_GUARD_MIN_COLS,
    height: process.stdout.rows ?? TERMINAL_GUARD_MIN_ROWS,
  };
}

// Module-level signal — created once, shared by all consumers.
// createSignal works outside a reactive root; without an owner the signal
// lives for the process lifetime, which is exactly what we want here.
const [terminalDimensions, setTerminalDimensions] = createSignal<TerminalDimensions>(
  readDimensions(),
);

export { terminalDimensions };

/**
 * Single source of truth for the "terminal too small" check.
 * Both TerminalGuard (display) and App (keybindings) consume this
 * so the threshold logic is never duplicated.
 */
export function isTooSmall(): boolean {
  if (!process.stdout.isTTY) return false;
  const d = terminalDimensions();
  return d.width < TERMINAL_GUARD_MIN_COLS || d.height < TERMINAL_GUARD_MIN_ROWS;
}

// ---------------------------------------------------------------------------
// Test utility — import only in test files.
// ---------------------------------------------------------------------------

/**
 * Directly set terminal dimensions, bypassing process.stdout and the resize
 * debounce. Allows unit tests to control the signal value without depending
 * on isTTY state or actual OS resize events.
 */
export const _setDimensionsForTesting = setTerminalDimensions;

// Only register a resize listener when running in a real TTY (not piped/CI).
// In non-TTY contexts the initial value (read above) is used permanently.
if (process.stdout.isTTY) {
  let debounceTimer: ReturnType<typeof setTimeout> | null = null;

  process.stdout.on("resize", () => {
    if (debounceTimer !== null) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      setTerminalDimensions(readDimensions());
      debounceTimer = null;
    }, RESIZE_DEBOUNCE_MS);
  });
}
