/**
 * Tests for the shared LruCache with O(n) eviction.
 *
 * Extends the original swr-lru tests to cover the extracted shared module.
 * @see Issue #3102, Decisions 7A + 16A
 */

import { describe, it, expect, beforeEach } from "bun:test";
import { LruCache } from "../../src/shared/utils/lru-cache.js";

describe("LruCache", () => {
  let cache: LruCache;

  beforeEach(() => {
    cache = new LruCache(5);
  });

  it("stores and retrieves entries", () => {
    cache.set("a", { data: 1, fetchedAt: 100 });
    expect(cache.get("a")?.data).toBe(1);
  });

  it("returns undefined for missing keys", () => {
    expect(cache.get("missing")).toBeUndefined();
  });

  it("reports has() correctly", () => {
    cache.set("a", { data: 1, fetchedAt: 100 });
    expect(cache.has("a")).toBe(true);
    expect(cache.has("b")).toBe(false);
  });

  it("deletes entries", () => {
    cache.set("a", { data: 1, fetchedAt: 100 });
    cache.delete("a");
    expect(cache.get("a")).toBeUndefined();
    expect(cache.has("a")).toBe(false);
  });

  it("clears all entries", () => {
    cache.set("a", { data: 1, fetchedAt: 100 });
    cache.set("b", { data: 2, fetchedAt: 100 });
    cache.clear();
    expect(cache.size).toBe(0);
    expect(cache.get("a")).toBeUndefined();
  });

  it("reports size correctly", () => {
    expect(cache.size).toBe(0);
    cache.set("a", { data: 1, fetchedAt: 100 });
    expect(cache.size).toBe(1);
    cache.set("b", { data: 2, fetchedAt: 100 });
    expect(cache.size).toBe(2);
  });

  it("evicts least-recently-used entry when exceeding maxSize", () => {
    // Fill to capacity (5)
    for (let i = 0; i < 5; i++) {
      cache.set(`key-${i}`, { data: i, fetchedAt: 100 });
    }
    expect(cache.size).toBe(5);

    // Adding one more should evict key-0 (oldest)
    cache.set("key-5", { data: 5, fetchedAt: 100 });
    expect(cache.size).toBe(5);
    expect(cache.get("key-0")).toBeUndefined();
    expect(cache.get("key-5")?.data).toBe(5);
  });

  it("promotes accessed entries (LRU behavior)", () => {
    // Fill to capacity
    for (let i = 0; i < 5; i++) {
      cache.set(`key-${i}`, { data: i, fetchedAt: 100 });
    }

    // Access key-0 to promote it
    cache.get("key-0");

    // Add a new entry — key-1 should be evicted (oldest non-accessed)
    cache.set("key-5", { data: 5, fetchedAt: 100 });
    expect(cache.get("key-0")?.data).toBe(0); // survived (was accessed)
    expect(cache.get("key-1")).toBeUndefined(); // evicted
  });

  it("evicts multiple entries when multiple are added past capacity", () => {
    for (let i = 0; i < 5; i++) {
      cache.set(`key-${i}`, { data: i, fetchedAt: 100 });
    }

    // Add 3 more — should evict 3 oldest
    cache.set("key-5", { data: 5, fetchedAt: 100 });
    cache.set("key-6", { data: 6, fetchedAt: 100 });
    cache.set("key-7", { data: 7, fetchedAt: 100 });

    expect(cache.get("key-0")).toBeUndefined();
    expect(cache.get("key-1")).toBeUndefined();
    expect(cache.get("key-2")).toBeUndefined();
    expect(cache.get("key-3")?.data).toBe(3);
    expect(cache.get("key-7")?.data).toBe(7);
  });

  it("overwrites existing entry without increasing size", () => {
    cache.set("a", { data: 1, fetchedAt: 100 });
    cache.set("a", { data: 2, fetchedAt: 200 });
    expect(cache.size).toBe(1);
    expect(cache.get("a")?.data).toBe(2);
    expect(cache.get("a")?.fetchedAt).toBe(200);
  });

  it("uses default maxSize of 200 when not specified", () => {
    const defaultCache = new LruCache();
    for (let i = 0; i < 205; i++) {
      defaultCache.set(`key-${i}`, { data: i, fetchedAt: 100 });
    }
    // Oldest 5 entries should be evicted
    expect(defaultCache.get("key-0")).toBeUndefined();
    expect(defaultCache.get("key-4")).toBeUndefined();
    expect(defaultCache.get("key-5")).toBeDefined();
    expect(defaultCache.get("key-204")?.data).toBe(204);
  });
});
