/**
 * Exhaustive truth table for brick state → allowed actions mapping.
 *
 * Tests all 7 FSM states × 5 possible actions = 35 cases.
 * This is the highest-risk logic in the TUI — a wrong guard means
 * the operator can trigger an invalid transition.
 */

import { describe, it, expect } from "bun:test";
import {
  allowedActionsForState,
  stateIndicator,
  BRICK_STATE,
  type BrickAction,
} from "../../src/shared/brick-states.js";

// ---------------------------------------------------------------------------
// allowedActionsForState — exhaustive truth table
// ---------------------------------------------------------------------------

describe("allowedActionsForState", () => {
  // Truth table: [state, expected allowed actions]
  const TRUTH_TABLE: readonly [string, readonly BrickAction[]][] = [
    [BRICK_STATE.REGISTERED, ["mount"]],
    [BRICK_STATE.STARTING, []],
    [BRICK_STATE.ACTIVE, ["unmount"]],
    [BRICK_STATE.STOPPING, []],
    [BRICK_STATE.UNMOUNTED, ["mount", "remount", "unregister"]],
    [BRICK_STATE.UNREGISTERED, []],
    [BRICK_STATE.FAILED, ["reset"]],
  ];

  for (const [state, expectedActions] of TRUTH_TABLE) {
    it(`${state} → [${expectedActions.join(", ") || "none"}]`, () => {
      const allowed = allowedActionsForState(state);
      expect(allowed.size).toBe(expectedActions.length);
      for (const action of expectedActions) {
        expect(allowed.has(action)).toBe(true);
      }
    });
  }

  // Verify disallowed actions explicitly for each state
  const ALL_ACTIONS: readonly BrickAction[] = ["mount", "unmount", "remount", "reset", "unregister"];

  it("registered disallows unmount, remount, reset, unregister", () => {
    const allowed = allowedActionsForState(BRICK_STATE.REGISTERED);
    expect(allowed.has("unmount")).toBe(false);
    expect(allowed.has("remount")).toBe(false);
    expect(allowed.has("reset")).toBe(false);
    expect(allowed.has("unregister")).toBe(false);
  });

  it("active disallows mount, remount, reset, unregister", () => {
    const allowed = allowedActionsForState(BRICK_STATE.ACTIVE);
    expect(allowed.has("mount")).toBe(false);
    expect(allowed.has("remount")).toBe(false);
    expect(allowed.has("reset")).toBe(false);
    expect(allowed.has("unregister")).toBe(false);
  });

  it("unmounted disallows unmount and reset", () => {
    const allowed = allowedActionsForState(BRICK_STATE.UNMOUNTED);
    expect(allowed.has("unmount")).toBe(false);
    expect(allowed.has("reset")).toBe(false);
  });

  it("unmounted allows mount, remount, and unregister", () => {
    const allowed = allowedActionsForState(BRICK_STATE.UNMOUNTED);
    expect(allowed.has("mount")).toBe(true);
    expect(allowed.has("remount")).toBe(true);
    expect(allowed.has("unregister")).toBe(true);
  });

  it("failed disallows mount, unmount, remount, unregister", () => {
    const allowed = allowedActionsForState(BRICK_STATE.FAILED);
    expect(allowed.has("mount")).toBe(false);
    expect(allowed.has("unmount")).toBe(false);
    expect(allowed.has("remount")).toBe(false);
    expect(allowed.has("unregister")).toBe(false);
  });

  it("transient states (starting, stopping) disallow all actions", () => {
    for (const state of [BRICK_STATE.STARTING, BRICK_STATE.STOPPING]) {
      const allowed = allowedActionsForState(state);
      for (const action of ALL_ACTIONS) {
        expect(allowed.has(action)).toBe(false);
      }
    }
  });

  it("terminal state (unregistered) disallows all actions", () => {
    const allowed = allowedActionsForState(BRICK_STATE.UNREGISTERED);
    for (const action of ALL_ACTIONS) {
      expect(allowed.has(action)).toBe(false);
    }
  });

  it("unknown state returns empty set", () => {
    const allowed = allowedActionsForState("bogus_state");
    expect(allowed.size).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// stateIndicator — display mapping
// ---------------------------------------------------------------------------

describe("stateIndicator", () => {
  it("maps all 7 backend states to indicators", () => {
    expect(stateIndicator(BRICK_STATE.REGISTERED)).toBe("[RG]");
    expect(stateIndicator(BRICK_STATE.STARTING)).toBe("[..]");
    expect(stateIndicator(BRICK_STATE.ACTIVE)).toBe("[ON]");
    expect(stateIndicator(BRICK_STATE.STOPPING)).toBe("[..]");
    expect(stateIndicator(BRICK_STATE.UNMOUNTED)).toBe("[UM]");
    expect(stateIndicator(BRICK_STATE.UNREGISTERED)).toBe("[--]");
    expect(stateIndicator(BRICK_STATE.FAILED)).toBe("[!!]");
  });

  it("returns [??] for unknown states", () => {
    expect(stateIndicator("bogus")).toBe("[??]");
    expect(stateIndicator("")).toBe("[??]");
  });
});
