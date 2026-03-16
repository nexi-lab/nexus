/**
 * Tests for LRU eviction in the SWR cache.
 *
 * Tests the cache module directly (not the React hook).
 *
 * @see Issue #3066, Decision 16A
 */

import { describe, it, expect, beforeEach } from "bun:test";
import { swrCache } from "../../src/shared/hooks/use-swr.js";

describe("SWR cache LRU eviction", () => {
  beforeEach(() => {
    swrCache.clear();
  });

  it("stores and retrieves entries", () => {
    swrCache.set("key1", { data: "value1", fetchedAt: Date.now() });
    expect(swrCache.get("key1")?.data).toBe("value1");
  });

  it("returns undefined for missing keys", () => {
    expect(swrCache.get("missing")).toBeUndefined();
  });

  it("deletes entries", () => {
    swrCache.set("key1", { data: "value1", fetchedAt: Date.now() });
    swrCache.delete("key1");
    expect(swrCache.get("key1")).toBeUndefined();
  });

  it("evicts oldest entries when exceeding maxSize", () => {
    // The default maxSize is 200. Let's verify with a smaller test scope.
    for (let i = 0; i < 205; i++) {
      swrCache.set(`key-${i}`, { data: i, fetchedAt: Date.now() });
    }

    // Oldest entries should be evicted
    expect(swrCache.get("key-0")).toBeUndefined();
    expect(swrCache.get("key-4")).toBeUndefined();

    // Newest should still exist
    expect(swrCache.get("key-204")?.data).toBe(204);
  });

  it("clears all entries", () => {
    swrCache.set("a", { data: 1, fetchedAt: Date.now() });
    swrCache.set("b", { data: 2, fetchedAt: Date.now() });
    swrCache.clear();
    expect(swrCache.get("a")).toBeUndefined();
    expect(swrCache.get("b")).toBeUndefined();
  });

  it("updates access time on get (LRU behavior)", () => {
    // Fill to near capacity
    for (let i = 0; i < 200; i++) {
      swrCache.set(`key-${i}`, { data: i, fetchedAt: Date.now() });
    }

    // Access key-0 to mark it as recently used
    swrCache.get("key-0");

    // Add more entries to trigger eviction
    for (let i = 200; i < 210; i++) {
      swrCache.set(`key-${i}`, { data: i, fetchedAt: Date.now() });
    }

    // key-0 should survive because it was recently accessed
    expect(swrCache.get("key-0")?.data).toBe(0);

    // key-1 through key-9 should be evicted (they were oldest and not accessed)
    expect(swrCache.get("key-1")).toBeUndefined();
  });
});
