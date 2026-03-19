/**
 * Tests for error-store — centralized structured error state.
 *
 * Written test-first (Decision 10A).
 */

import { describe, it, expect, beforeEach } from "bun:test";
import {
  useErrorStore,
  type AppError,
  type ErrorCategory,
} from "../../src/stores/error-store.js";

describe("ErrorStore", () => {
  beforeEach(() => {
    useErrorStore.setState({ errors: [] });
  });

  // ===========================================================================
  // Push errors
  // ===========================================================================

  describe("pushError", () => {
    it("adds a structured error", () => {
      useErrorStore.getState().pushError({
        message: "Connection refused",
        category: "network",
      });

      const errors = useErrorStore.getState().errors;
      expect(errors).toHaveLength(1);
      expect(errors[0]!.message).toBe("Connection refused");
      expect(errors[0]!.category).toBe("network");
      expect(errors[0]!.dismissable).toBe(true);
    });

    it("assigns unique IDs to errors", () => {
      useErrorStore.getState().pushError({ message: "Error 1", category: "network" });
      useErrorStore.getState().pushError({ message: "Error 2", category: "server" });

      const errors = useErrorStore.getState().errors;
      expect(errors[0]!.id).not.toBe(errors[1]!.id);
    });

    it("sets timestamp on push", () => {
      const before = Date.now();
      useErrorStore.getState().pushError({ message: "Err", category: "network" });
      const after = Date.now();

      const ts = useErrorStore.getState().errors[0]!.timestamp;
      expect(ts).toBeGreaterThanOrEqual(before);
      expect(ts).toBeLessThanOrEqual(after);
    });

    it("respects non-dismissable flag", () => {
      useErrorStore.getState().pushError({
        message: "Fatal",
        category: "server",
        dismissable: false,
      });

      expect(useErrorStore.getState().errors[0]!.dismissable).toBe(false);
    });

    it("stores optional source panel", () => {
      useErrorStore.getState().pushError({
        message: "Error",
        category: "validation",
        source: "payments",
      });

      expect(useErrorStore.getState().errors[0]!.source).toBe("payments");
    });

    it("stores optional retry action", () => {
      const retry = () => {};
      useErrorStore.getState().pushError({
        message: "Error",
        category: "network",
        retryAction: retry,
      });

      expect(useErrorStore.getState().errors[0]!.retryAction).toBe(retry);
    });
  });

  // ===========================================================================
  // Dismiss
  // ===========================================================================

  describe("dismissError", () => {
    it("removes error by ID", () => {
      useErrorStore.getState().pushError({ message: "A", category: "network" });
      useErrorStore.getState().pushError({ message: "B", category: "server" });

      const idA = useErrorStore.getState().errors[0]!.id;
      useErrorStore.getState().dismissError(idA);

      const remaining = useErrorStore.getState().errors;
      expect(remaining).toHaveLength(1);
      expect(remaining[0]!.message).toBe("B");
    });

    it("no-ops for unknown ID", () => {
      useErrorStore.getState().pushError({ message: "A", category: "network" });
      useErrorStore.getState().dismissError("nonexistent");
      expect(useErrorStore.getState().errors).toHaveLength(1);
    });
  });

  describe("dismissAll", () => {
    it("removes all dismissable errors", () => {
      useErrorStore.getState().pushError({ message: "A", category: "network" });
      useErrorStore.getState().pushError({ message: "B", category: "server", dismissable: false });
      useErrorStore.getState().pushError({ message: "C", category: "validation" });

      useErrorStore.getState().dismissAll();

      const remaining = useErrorStore.getState().errors;
      expect(remaining).toHaveLength(1);
      expect(remaining[0]!.message).toBe("B");
    });

    it("clears everything when all are dismissable", () => {
      useErrorStore.getState().pushError({ message: "A", category: "network" });
      useErrorStore.getState().pushError({ message: "B", category: "server" });

      useErrorStore.getState().dismissAll();
      expect(useErrorStore.getState().errors).toHaveLength(0);
    });
  });

  // ===========================================================================
  // Max errors (overflow protection)
  // ===========================================================================

  describe("max errors cap", () => {
    it("evicts oldest when exceeding max (50)", () => {
      for (let i = 0; i < 55; i++) {
        useErrorStore.getState().pushError({
          message: `Error ${i}`,
          category: "network",
        });
      }

      const errors = useErrorStore.getState().errors;
      expect(errors.length).toBeLessThanOrEqual(50);
      // Oldest should have been evicted
      expect(errors[0]!.message).toBe("Error 5");
      expect(errors[errors.length - 1]!.message).toBe("Error 54");
    });
  });

  // ===========================================================================
  // Filter by source
  // ===========================================================================

  describe("getErrorsForSource", () => {
    it("returns only errors for given source panel", () => {
      useErrorStore.getState().pushError({ message: "A", category: "network", source: "files" });
      useErrorStore.getState().pushError({ message: "B", category: "server", source: "payments" });
      useErrorStore.getState().pushError({ message: "C", category: "network", source: "files" });

      const fileErrors = useErrorStore.getState().getErrorsForSource("files");
      expect(fileErrors).toHaveLength(2);
      expect(fileErrors[0]!.message).toBe("A");
      expect(fileErrors[1]!.message).toBe("C");
    });

    it("returns empty array for unknown source", () => {
      useErrorStore.getState().pushError({ message: "A", category: "network", source: "files" });
      expect(useErrorStore.getState().getErrorsForSource("agents")).toEqual([]);
    });
  });

  // ===========================================================================
  // Category helpers
  // ===========================================================================

  describe("hasErrors", () => {
    it("returns false when no errors", () => {
      expect(useErrorStore.getState().hasErrors()).toBe(false);
    });

    it("returns true when errors exist", () => {
      useErrorStore.getState().pushError({ message: "A", category: "network" });
      expect(useErrorStore.getState().hasErrors()).toBe(true);
    });
  });
});
