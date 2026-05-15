/**
 * Keyboard state → binding-set selector for App() (#3501).
 *
 * Extracted from app.tsx so the branch logic can be unit-tested without
 * mounting the full application. The actual handler implementations stay
 * in app.tsx; this module only decides *which set* is active.
 *
 * Priority (highest first):
 * 1. resize     — terminal below minimum size; only q:quit is active
 * 2. pre-connection — server unavailable; only q:quit is active
 * 3. overlay    — an overlay is open; only overlay-dismiss keys active
 * 4. normal     — full keybinding set
 */

export type KeyboardState = {
  readonly terminalTooSmall: boolean;
  readonly showPreConnection: boolean;
  readonly overlayOpen: boolean;
};

export type KeyBindingBranch = "resize" | "pre-connection" | "overlay" | "normal";

/** Determine which keyboard binding set is active given the current UI state. */
export function selectBranch(state: KeyboardState): KeyBindingBranch {
  if (state.terminalTooSmall) return "resize";
  if (state.showPreConnection) return "pre-connection";
  if (state.overlayOpen) return "overlay";
  return "normal";
}
