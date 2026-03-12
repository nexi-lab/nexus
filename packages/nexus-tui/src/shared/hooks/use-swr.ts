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

// Module-level cache shared across hook instances
const cache = new Map<string, CacheEntry<unknown>>();

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
