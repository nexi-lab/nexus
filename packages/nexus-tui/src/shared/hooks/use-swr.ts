/**
 * Generic stale-while-revalidate hook for data fetching.
 *
 * - Immediately returns cached data if available (even if stale)
 * - Triggers background revalidation if data is stale
 * - Reports loading/error states
 */

import { useState, useEffect, useRef, useCallback } from "react";

interface SwrOptions {
  /** Time in ms before cached data is considered stale. Default: 30000 */
  readonly ttlMs?: number;
  /** Whether to fetch immediately on mount. Default: true */
  readonly enabled?: boolean;
}

interface SwrResult<T> {
  readonly data: T | undefined;
  readonly error: Error | undefined;
  readonly isLoading: boolean;
  readonly isStale: boolean;
  readonly mutate: () => void;
}

interface CacheEntry<T> {
  data: T;
  fetchedAt: number;
}

// =============================================================================
// LRU cache with eviction (Decision 16A)
// =============================================================================

const MAX_CACHE_SIZE = 200;

let accessCounter = 0;

class LruCache {
  private readonly _map = new Map<string, { entry: CacheEntry<unknown>; accessOrder: number }>();

  get(key: string): CacheEntry<unknown> | undefined {
    const item = this._map.get(key);
    if (!item) return undefined;
    // Update access order for LRU tracking (monotonic counter avoids Date.now() ties)
    item.accessOrder = ++accessCounter;
    return item.entry;
  }

  set(key: string, entry: CacheEntry<unknown>): void {
    this._map.set(key, { entry, accessOrder: ++accessCounter });
    this._evictIfNeeded();
  }

  delete(key: string): void {
    this._map.delete(key);
  }

  clear(): void {
    this._map.clear();
  }

  private _evictIfNeeded(): void {
    if (this._map.size <= MAX_CACHE_SIZE) return;

    // Find and remove least-recently-accessed entries
    const entries = [...this._map.entries()].sort(
      (a, b) => a[1].accessOrder - b[1].accessOrder,
    );

    const toRemove = this._map.size - MAX_CACHE_SIZE;
    for (let i = 0; i < toRemove; i++) {
      this._map.delete(entries[i]![0]);
    }
  }
}

// Module-level cache shared across hook instances
/** @internal Exported for testing LRU behavior */
export const swrCache = new LruCache();

// Keep backward-compatible local alias
const cache = swrCache;

export function useSwr<T>(
  key: string,
  fetcher: () => Promise<T>,
  options?: SwrOptions,
): SwrResult<T> {
  const ttlMs = options?.ttlMs ?? 30_000;
  const enabled = options?.enabled ?? true;

  const [data, setData] = useState<T | undefined>(() => {
    const cached = cache.get(key) as CacheEntry<T> | undefined;
    return cached?.data;
  });
  const [error, setError] = useState<Error | undefined>();
  const [isLoading, setIsLoading] = useState(false);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const isStale = (() => {
    const cached = cache.get(key);
    if (!cached) return true;
    return Date.now() - cached.fetchedAt > ttlMs;
  })();

  const doFetch = useCallback(async () => {
    setIsLoading(true);
    setError(undefined);
    try {
      const result = await fetcherRef.current();
      cache.set(key, { data: result, fetchedAt: Date.now() });
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      setIsLoading(false);
    }
  }, [key]);

  useEffect(() => {
    if (!enabled) return;
    if (isStale) {
      doFetch();
    }
  }, [key, enabled, isStale, doFetch]);

  const mutate = useCallback(() => {
    cache.delete(key);
    doFetch();
  }, [key, doFetch]);

  return { data, error, isLoading, isStale, mutate };
}
