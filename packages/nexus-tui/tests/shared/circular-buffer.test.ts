import { describe, it, expect } from "bun:test";
import { CircularBuffer } from "../../src/shared/lib/circular-buffer.js";

describe("CircularBuffer", () => {
  describe("construction", () => {
    it("creates with specified capacity", () => {
      const buf = new CircularBuffer<number>(5);
      expect(buf.capacity).toBe(5);
      expect(buf.size).toBe(0);
    });

    it("throws for capacity < 1", () => {
      expect(() => new CircularBuffer(0)).toThrow("capacity must be >= 1");
      expect(() => new CircularBuffer(-1)).toThrow("capacity must be >= 1");
    });
  });

  describe("push and get", () => {
    it("adds items and retrieves by index", () => {
      const buf = new CircularBuffer<string>(3);
      buf.push("a");
      buf.push("b");
      expect(buf.get(0)).toBe("a");
      expect(buf.get(1)).toBe("b");
      expect(buf.size).toBe(2);
    });

    it("fills to capacity", () => {
      const buf = new CircularBuffer<number>(3);
      buf.push(1);
      buf.push(2);
      buf.push(3);
      expect(buf.size).toBe(3);
      expect(buf.toArray()).toEqual([1, 2, 3]);
    });
  });

  describe("overflow (eviction)", () => {
    it("evicts oldest item when full", () => {
      const buf = new CircularBuffer<number>(3);
      buf.push(1);
      buf.push(2);
      buf.push(3);
      buf.push(4); // evicts 1
      expect(buf.size).toBe(3);
      expect(buf.toArray()).toEqual([2, 3, 4]);
    });

    it("evicts multiple items correctly", () => {
      const buf = new CircularBuffer<number>(3);
      for (let i = 1; i <= 7; i++) buf.push(i);
      // Should have [5, 6, 7]
      expect(buf.toArray()).toEqual([5, 6, 7]);
      expect(buf.size).toBe(3);
    });

    it("tracks totalAdded across overflows", () => {
      const buf = new CircularBuffer<number>(3);
      for (let i = 1; i <= 7; i++) buf.push(i);
      expect(buf.totalAdded).toBe(7);
      expect(buf.evictedCount).toBe(4);
      expect(buf.hasOverflowed).toBe(true);
    });

    it("reports hasOverflowed = false when not full", () => {
      const buf = new CircularBuffer<number>(5);
      buf.push(1);
      buf.push(2);
      expect(buf.hasOverflowed).toBe(false);
      expect(buf.evictedCount).toBe(0);
    });
  });

  describe("get edge cases", () => {
    it("throws for negative index", () => {
      const buf = new CircularBuffer<number>(3);
      buf.push(1);
      expect(() => buf.get(-1)).toThrow("out of range");
    });

    it("throws for index >= size", () => {
      const buf = new CircularBuffer<number>(3);
      buf.push(1);
      expect(() => buf.get(1)).toThrow("out of range");
      expect(() => buf.get(3)).toThrow("out of range");
    });

    it("throws for empty buffer", () => {
      const buf = new CircularBuffer<number>(3);
      expect(() => buf.get(0)).toThrow("out of range");
    });
  });

  describe("clear", () => {
    it("resets all state", () => {
      const buf = new CircularBuffer<number>(3);
      buf.push(1);
      buf.push(2);
      buf.push(3);
      buf.push(4);

      buf.clear();
      expect(buf.size).toBe(0);
      expect(buf.totalAdded).toBe(0);
      expect(buf.evictedCount).toBe(0);
      expect(buf.hasOverflowed).toBe(false);
      expect(buf.toArray()).toEqual([]);
    });

    it("allows reuse after clear", () => {
      const buf = new CircularBuffer<number>(2);
      buf.push(1);
      buf.push(2);
      buf.clear();
      buf.push(10);
      expect(buf.toArray()).toEqual([10]);
    });
  });

  describe("iteration", () => {
    it("iterates oldest to newest", () => {
      const buf = new CircularBuffer<number>(3);
      buf.push(1);
      buf.push(2);
      buf.push(3);
      const items: number[] = [];
      for (const item of buf) items.push(item);
      expect(items).toEqual([1, 2, 3]);
    });

    it("iterates correctly after overflow", () => {
      const buf = new CircularBuffer<number>(3);
      for (let i = 1; i <= 5; i++) buf.push(i);
      const items: number[] = [];
      for (const item of buf) items.push(item);
      expect(items).toEqual([3, 4, 5]);
    });

    it("spread works", () => {
      const buf = new CircularBuffer<string>(2);
      buf.push("a");
      buf.push("b");
      expect([...buf]).toEqual(["a", "b"]);
    });

    it("empty buffer iterates zero times", () => {
      const buf = new CircularBuffer<number>(3);
      expect([...buf]).toEqual([]);
    });
  });

  describe("capacity = 1", () => {
    it("holds exactly one item", () => {
      const buf = new CircularBuffer<string>(1);
      buf.push("a");
      expect(buf.get(0)).toBe("a");
      buf.push("b");
      expect(buf.get(0)).toBe("b");
      expect(buf.size).toBe(1);
      expect(buf.totalAdded).toBe(2);
    });
  });
});
