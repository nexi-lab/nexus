/**
 * Brick availability hooks.
 *
 * Provides a single useBricksAvailable(bricks) hook (OR semantics: available
 * if any of the listed bricks is enabled) and a checkBricksAvailable pure
 * function for testing.
 *
 * Replaces the previous useBrickAvailable / useAnyBrickAvailable pair, which
 * violated React's Rules of Hooks when BrickGate called them conditionally.
 */

import { useGlobalStore } from "../../stores/global-store.js";

export interface BrickAvailability {
  readonly available: boolean;
  readonly loading: boolean;
}

/**
 * Pure function for testability: computes brick availability without hooks.
 *
 * - Returns { available: false, loading: true } while features are loading,
 *   to prevent a flash of "not available" before the feature list arrives.
 * - Returns { available: true } if any brick in `bricks` is in enabledBricks.
 */
export function checkBricksAvailable(
  enabledBricks: readonly string[],
  featuresLoaded: boolean,
  bricks: readonly string[],
): BrickAvailability {
  return {
    available: featuresLoaded && bricks.some((b) => enabledBricks.includes(b)),
    loading: !featuresLoaded,
  };
}

/**
 * Check if any of the given bricks are enabled in the current deployment (OR
 * semantics). Always called unconditionally — safe to use regardless of how
 * many bricks are passed.
 */
export function useBricksAvailable(bricks: readonly string[]): BrickAvailability {
  const enabledBricks = useGlobalStore((s) => s.enabledBricks);
  const featuresLoaded = useGlobalStore((s) => s.featuresLoaded);
  return checkBricksAvailable(enabledBricks, featuresLoaded, bricks);
}

/**
 * Convenience wrapper for the common single-brick case.
 * Calls useBricksAvailable([brickName]) so hook call order is always stable.
 */
export function useBrickAvailable(brickName: string): BrickAvailability {
  return useBricksAvailable([brickName]);
}
