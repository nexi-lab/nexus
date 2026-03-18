/**
 * Generic stale-while-revalidate hook for data fetching.
 *
 * - Immediately returns cached data if available (even if stale)
 * - Triggers background revalidation if data is stale
 * - Reports loading/error states
 * - Aborts in-flight requests on key change or unmount (Issue #3102)
 */

import { useState, useEffect, useRef, useCallback } from "react";
import { LruCache } from "../utils/lru-cache.js";

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

// =============================================================================
// Module-level cache shared across hook instances (Decision 7A)
// =============================================================================

/** @internal Exported for testing LRU behavior */
export const swrCache = new LruCache(200);

export function useSwr<T>(
  key: string,
  fetcher: (signal: AbortSignal) => Promise<T>,
  options?: SwrOptions,
): SwrResult<T> {
  const ttlMs = options?.ttlMs ?? 30_000;
  const enabled = options?.enabled ?? true;

  const [data, setData] = useState<T | undefined>(() => {
    const cached = swrCache.get(key) as { data: T; fetchedAt: number } | undefined;
    return cached?.data;
  });
  const [error, setError] = useState<Error | undefined>();
  const [isLoading, setIsLoading] = useState(false);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // Track the active key so in-flight fetches for stale keys are discarded
  const activeKeyRef = useRef(key);
  activeKeyRef.current = key;

  // Track the active AbortController for cancellation (Issue #3102, Decision 3A)
  const controllerRef = useRef<AbortController | null>(null);

  // Reset data to cached value (or undefined) when key changes
  useEffect(() => {
    const cached = swrCache.get(key) as { data: T; fetchedAt: number } | undefined;
    setData(cached?.data);
  }, [key]);

  const isStale = (() => {
    const cached = swrCache.get(key);
    if (!cached) return true;
    return Date.now() - cached.fetchedAt > ttlMs;
  })();

  const doFetch = useCallback(async () => {
    const fetchKey = key; // capture for closure

    // Abort any previous in-flight fetch
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;

    setIsLoading(true);
    setError(undefined);
    try {
      const result = await fetcherRef.current(controller.signal);
      // Only update if this key is still the active one
      if (activeKeyRef.current !== fetchKey) return;
      swrCache.set(key, { data: result, fetchedAt: Date.now() });
      setData(result);
    } catch (err) {
      // Suppress AbortError — it's expected when we cancel
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (activeKeyRef.current !== fetchKey) return;
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      if (activeKeyRef.current === fetchKey) {
        setIsLoading(false);
      }
    }
  }, [key]);

  useEffect(() => {
    if (!enabled) return;
    if (isStale) {
      doFetch();
    }

    // Abort in-flight request on unmount or key change
    return () => {
      controllerRef.current?.abort();
    };
  }, [key, enabled, isStale, doFetch]);

  const mutate = useCallback(() => {
    swrCache.delete(key);
    doFetch();
  }, [key, doFetch]);

  return { data, error, isLoading, isStale, mutate };
}
