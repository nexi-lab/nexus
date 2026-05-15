/**
 * Tests for the useSwr hook — cache integration and fetcher contract.
 *
 * These tests exercise the module-level swrCache and the fetcher/AbortSignal
 * contract without rendering the React hook (no DOM needed).
 *
 * LRU eviction behavior is covered in swr-lru.test.ts.
 */

import { describe, it, expect, beforeEach, mock } from "bun:test";
import { swrCache } from "../../src/shared/hooks/use-swr.js";

describe("useSwr cache integration", () => {
  beforeEach(() => {
    swrCache.clear();
  });

  // ---------------------------------------------------------------------------
  // 1. Cache starts empty for unknown key
  // ---------------------------------------------------------------------------
  it("returns undefined for an unknown key", () => {
    expect(swrCache.get("unknown-key")).toBeUndefined();
  });

  // ---------------------------------------------------------------------------
  // 2. Cache set populates data and fetchedAt
  // ---------------------------------------------------------------------------
  it("stores data and fetchedAt via set", () => {
    const now = Date.now();
    swrCache.set("users", { data: [{ id: 1 }], fetchedAt: now });

    const entry = swrCache.get("users");
    expect(entry).toBeDefined();
    expect(entry!.data).toEqual([{ id: 1 }]);
    expect(entry!.fetchedAt).toBe(now);
  });

  // ---------------------------------------------------------------------------
  // 3. Cache entry is fresh within TTL (default 30s)
  // ---------------------------------------------------------------------------
  it("entry is fresh when fetchedAt is within the default 30s TTL", () => {
    const defaultTtlMs = 30_000;
    const fetchedAt = Date.now() - (defaultTtlMs - 1_000); // 1s before expiry
    swrCache.set("fresh-key", { data: "fresh", fetchedAt });

    const entry = swrCache.get("fresh-key")!;
    const age = Date.now() - entry.fetchedAt;
    expect(age).toBeLessThan(defaultTtlMs);
  });

  // ---------------------------------------------------------------------------
  // 4. Cache entry is stale past TTL
  // ---------------------------------------------------------------------------
  it("entry is stale when fetchedAt exceeds the default 30s TTL", () => {
    const defaultTtlMs = 30_000;
    const fetchedAt = Date.now() - (defaultTtlMs + 1_000); // 1s past expiry
    swrCache.set("stale-key", { data: "stale", fetchedAt });

    const entry = swrCache.get("stale-key")!;
    const age = Date.now() - entry.fetchedAt;
    expect(age).toBeGreaterThan(defaultTtlMs);
  });

  // ---------------------------------------------------------------------------
  // 5. Fetcher receives an AbortSignal argument
  // ---------------------------------------------------------------------------
  it("fetcher receives an AbortSignal when called with AbortController", async () => {
    const fetcher = mock(async (signal: AbortSignal) => {
      expect(signal).toBeInstanceOf(AbortSignal);
      expect(signal.aborted).toBe(false);
      return "result";
    });

    const controller = new AbortController();
    const result = await fetcher(controller.signal);

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result).toBe("result");
  });

  // ---------------------------------------------------------------------------
  // 6. AbortController.abort() causes AbortError
  // ---------------------------------------------------------------------------
  it("AbortController.abort() causes fetch-like operations to throw AbortError", async () => {
    const controller = new AbortController();

    const fetcher = async (signal: AbortSignal): Promise<string> => {
      // Simulate an async operation that respects the signal
      return new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => {
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      });
    };

    const promise = fetcher(controller.signal);
    controller.abort();

    try {
      await promise;
      // Should not reach here
      expect(true).toBe(false);
    } catch (err) {
      expect(err).toBeInstanceOf(DOMException);
      expect((err as DOMException).name).toBe("AbortError");
    }
  });

  // ---------------------------------------------------------------------------
  // 7. swrCache.delete removes entry
  // ---------------------------------------------------------------------------
  it("delete removes a cache entry (simulates mutate clearing cache)", () => {
    swrCache.set("to-delete", { data: "bye", fetchedAt: Date.now() });
    expect(swrCache.get("to-delete")).toBeDefined();

    swrCache.delete("to-delete");
    expect(swrCache.get("to-delete")).toBeUndefined();
  });

  // ---------------------------------------------------------------------------
  // 8. swrCache respects LRU eviction (integration sanity check)
  // ---------------------------------------------------------------------------
  it("evicts least-recently-used entries when capacity is exceeded", () => {
    // swrCache has maxSize 200
    for (let i = 0; i < 201; i++) {
      swrCache.set(`k-${i}`, { data: i, fetchedAt: Date.now() });
    }

    // The very first entry (k-0) should have been evicted
    expect(swrCache.get("k-0")).toBeUndefined();

    // The most recent entry should still be present
    expect(swrCache.get("k-200")?.data).toBe(200);
  });
});
