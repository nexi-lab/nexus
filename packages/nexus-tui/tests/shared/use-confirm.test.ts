/**
 * Tests for useConfirm store — imperative confirmation dialog system.
 *
 * Written test-first (Decision 10A).
 */

import { describe, it, expect, beforeEach } from "bun:test";
import { useConfirmStore } from "../../src/shared/hooks/use-confirm.js";

describe("useConfirmStore", () => {
  beforeEach(() => {
    useConfirmStore.setState({
      visible: false,
      title: "",
      message: "",
      resolve: null,
    });
  });

  describe("confirm()", () => {
    it("makes dialog visible", () => {
      // Start confirm (don't await — it blocks until resolved)
      useConfirmStore.getState().confirm("Delete?", "This cannot be undone");
      expect(useConfirmStore.getState().visible).toBe(true);
      expect(useConfirmStore.getState().title).toBe("Delete?");
      expect(useConfirmStore.getState().message).toBe("This cannot be undone");

      // Resolve to prevent dangling promise
      useConfirmStore.getState().resolve?.(false);
    });

    it("resolves true on accept", async () => {
      const promise = useConfirmStore.getState().confirm("Delete?", "Are you sure?");

      // Simulate user pressing Y
      useConfirmStore.getState().resolve?.(true);

      const result = await promise;
      expect(result).toBe(true);
      expect(useConfirmStore.getState().visible).toBe(false);
    });

    it("resolves false on reject", async () => {
      const promise = useConfirmStore.getState().confirm("Delete?", "Are you sure?");

      // Simulate user pressing N/Escape
      useConfirmStore.getState().resolve?.(false);

      const result = await promise;
      expect(result).toBe(false);
      expect(useConfirmStore.getState().visible).toBe(false);
    });

    it("cleans up state after resolution", async () => {
      const promise = useConfirmStore.getState().confirm("Title", "Message");
      useConfirmStore.getState().resolve?.(true);
      await promise;

      expect(useConfirmStore.getState().visible).toBe(false);
      expect(useConfirmStore.getState().title).toBe("");
      expect(useConfirmStore.getState().message).toBe("");
      expect(useConfirmStore.getState().resolve).toBeNull();
    });
  });

  describe("concurrent confirms", () => {
    it("rejects previous confirm when new one arrives", async () => {
      // First confirm
      const promise1 = useConfirmStore.getState().confirm("First?", "...");

      // Second confirm replaces first — first should resolve false
      const promise2 = useConfirmStore.getState().confirm("Second?", "...");

      const result1 = await promise1;
      expect(result1).toBe(false);
      expect(useConfirmStore.getState().title).toBe("Second?");

      // Resolve second to clean up
      useConfirmStore.getState().resolve?.(true);
      const result2 = await promise2;
      expect(result2).toBe(true);
    });
  });
});
