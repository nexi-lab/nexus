/**
 * Tests for checkBricksAvailable pure function.
 *
 * Covers:
 * - Loading guard: returns loading=true while featuresLoaded=false
 * - Single brick: available when present, unavailable when absent
 * - Multi-brick OR semantics: available if any brick matches
 * - Empty bricks array: never available
 *
 * Tests use checkBricksAvailable directly (the pure function extracted for
 * testability) rather than the useBricksAvailable hook.
 */

import { describe, it, expect } from "bun:test";
import { checkBricksAvailable } from "../../src/shared/hooks/use-brick-available.js";

describe("checkBricksAvailable", () => {
  describe("loading guard", () => {
    it("returns loading=true when featuresLoaded is false", () => {
      const result = checkBricksAvailable([], false, ["any_brick"]);
      expect(result.loading).toBe(true);
    });

    it("returns available=false when featuresLoaded is false (even if brick present)", () => {
      const result = checkBricksAvailable(["my_brick"], false, ["my_brick"]);
      expect(result.available).toBe(false);
    });

    it("returns loading=false when featuresLoaded is true", () => {
      const result = checkBricksAvailable(["my_brick"], true, ["my_brick"]);
      expect(result.loading).toBe(false);
    });
  });

  describe("single brick", () => {
    it("returns available=true when brick is in enabledBricks", () => {
      const result = checkBricksAvailable(["storage", "agent_runtime"], true, ["storage"]);
      expect(result.available).toBe(true);
    });

    it("returns available=false when brick is not in enabledBricks", () => {
      const result = checkBricksAvailable(["storage"], true, ["agent_runtime"]);
      expect(result.available).toBe(false);
    });
  });

  describe("multi-brick OR semantics", () => {
    it("returns available=true when any brick matches", () => {
      const result = checkBricksAvailable(["storage"], true, ["agent_runtime", "storage"]);
      expect(result.available).toBe(true);
    });

    it("returns available=false when no brick matches", () => {
      const result = checkBricksAvailable(["storage"], true, ["agent_runtime", "delegation"]);
      expect(result.available).toBe(false);
    });

    it("returns available=true when all bricks match", () => {
      const result = checkBricksAvailable(["agent_runtime", "delegation"], true, ["agent_runtime", "delegation"]);
      expect(result.available).toBe(true);
    });
  });

  describe("edge cases", () => {
    it("returns available=false for empty bricks array", () => {
      const result = checkBricksAvailable(["storage", "agent_runtime"], true, []);
      expect(result.available).toBe(false);
    });

    it("returns available=false against empty enabledBricks", () => {
      const result = checkBricksAvailable([], true, ["storage"]);
      expect(result.available).toBe(false);
    });

    it("both empty arrays: available=false, loading=false", () => {
      const result = checkBricksAvailable([], true, []);
      expect(result.available).toBe(false);
      expect(result.loading).toBe(false);
    });
  });
});
