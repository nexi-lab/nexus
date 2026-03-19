/**
 * Brick FSM state constants and state-aware action logic.
 *
 * Single source of truth for brick lifecycle states — matches the backend
 * BrickState enum values exactly (see brick_lifecycle.py).
 */

import { brickStateColor } from "./theme.js";

// ---------------------------------------------------------------------------
// State constants (match backend BrickState enum values)
// ---------------------------------------------------------------------------

export const BRICK_STATE = {
  REGISTERED: "registered",
  STARTING: "starting",
  ACTIVE: "active",
  STOPPING: "stopping",
  UNMOUNTED: "unmounted",
  UNREGISTERED: "unregistered",
  FAILED: "failed",
} as const;

export type BrickStateValue = (typeof BRICK_STATE)[keyof typeof BRICK_STATE];

// ---------------------------------------------------------------------------
// Actions the TUI can trigger
// ---------------------------------------------------------------------------

export type BrickAction = "mount" | "unmount" | "remount" | "reset" | "unregister";

// ---------------------------------------------------------------------------
// State → allowed actions mapping
// ---------------------------------------------------------------------------

const STATE_ACTIONS: Readonly<Record<string, readonly BrickAction[]>> = {
  [BRICK_STATE.REGISTERED]: ["mount"],
  [BRICK_STATE.STARTING]: [],
  [BRICK_STATE.ACTIVE]: ["unmount"],
  [BRICK_STATE.STOPPING]: [],
  [BRICK_STATE.UNMOUNTED]: ["mount", "remount", "unregister"],
  [BRICK_STATE.UNREGISTERED]: [],
  [BRICK_STATE.FAILED]: ["reset"],
};

/**
 * Returns the set of lifecycle actions valid for a given brick state.
 *
 * Pure function — safe to call from render and easy to test exhaustively.
 */
export function allowedActionsForState(state: string): ReadonlySet<BrickAction> {
  const actions = STATE_ACTIONS[state];
  return new Set(actions ?? []);
}

// ---------------------------------------------------------------------------
// State → display indicator
// ---------------------------------------------------------------------------

/**
 * Short state indicator for the brick list sidebar.
 * Matches all 7 backend FSM states.
 */
export function stateIndicator(state: string): string {
  switch (state) {
    case BRICK_STATE.REGISTERED:
      return "[RG]";
    case BRICK_STATE.STARTING:
      return "[..]";
    case BRICK_STATE.ACTIVE:
      return "[ON]";
    case BRICK_STATE.STOPPING:
      return "[..]";
    case BRICK_STATE.UNMOUNTED:
      return "[UM]";
    case BRICK_STATE.UNREGISTERED:
      return "[--]";
    case BRICK_STATE.FAILED:
      return "[!!]";
    default:
      return "[??]";
  }
}

/**
 * Semantic color for a brick state indicator.
 * Returns a color string from the theme for use with foregroundColor prop.
 */
export function stateColor(state: string): string {
  return brickStateColor[state] ?? "gray";
}
