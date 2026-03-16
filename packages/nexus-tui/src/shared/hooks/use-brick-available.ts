/**
 * Hook to check if a brick is available (enabled) in the current deployment.
 *
 * Returns { available, loading } to distinguish "not loaded yet" from
 * "definitively disabled" (Decision 1A).
 */

import { useGlobalStore } from "../../stores/global-store.js";

interface BrickAvailability {
  readonly available: boolean;
  readonly loading: boolean;
}

/**
 * Check if a specific brick is enabled in the current deployment profile.
 *
 * During initial feature loading, returns { available: false, loading: true }
 * to prevent flash of "not available" messages.
 */
export function useBrickAvailable(brickName: string): BrickAvailability {
  const enabledBricks = useGlobalStore((s) => s.enabledBricks);
  const featuresLoaded = useGlobalStore((s) => s.featuresLoaded);

  return {
    available: enabledBricks.includes(brickName),
    loading: !featuresLoaded,
  };
}

/**
 * Check if any of the specified bricks are available (OR semantics).
 */
export function useAnyBrickAvailable(brickNames: readonly string[]): BrickAvailability {
  const enabledBricks = useGlobalStore((s) => s.enabledBricks);
  const featuresLoaded = useGlobalStore((s) => s.featuresLoaded);

  return {
    available: brickNames.some((name) => enabledBricks.includes(name)),
    loading: !featuresLoaded,
  };
}
