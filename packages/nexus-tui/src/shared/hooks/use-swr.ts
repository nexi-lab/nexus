/**
 * Generic stale-while-revalidate hook for data fetching.
 *
 * - Immediately returns cached data if available (even if stale)
 * - Triggers background revalidation if data is stale
 * - Reports loading/error states
 * - Aborts in-flight requests on key change or unmount (Issue #3102)
 */

import { createMemo, createResource, createSignal, onCleanup } from "solid-js";
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

type SwrKey = string | (() => string);

// =============================================================================
// Module-level cache shared across hook instances (Decision 7A)
// =============================================================================

/** @internal Exported for testing LRU behavior */
export const swrCache = new LruCache(200);

export function useSwr<T>(
  key: SwrKey,
  fetcher: (signal: AbortSignal) => Promise<T>,
  options?: SwrOptions,
): SwrResult<T> {
  const ttlMs = options?.ttlMs ?? 30_000;
  const enabled = options?.enabled ?? true;

  let controller: AbortController | null = null;
  const [refreshToken, setRefreshToken] = createSignal(0);
  const resolvedKey = createMemo(() => typeof key === "function" ? key() : key);
  const cacheEntry = createMemo(() => swrCache.get(resolvedKey()) as { data: T; fetchedAt: number } | undefined);
  const isStale = createMemo(() => {
    const cached = cacheEntry();
    if (!cached) return true;
    return Date.now() - cached.fetchedAt > ttlMs;
  });

  const [resource, { refetch }] = createResource(
    () => enabled ? { key: resolvedKey(), refreshToken: refreshToken() } : null,
    async (source) => {
      if (!source) return cacheEntry()?.data as T | undefined;
      const currentKey = source.key;
      const cached = swrCache.get(currentKey) as { data: T; fetchedAt: number } | undefined;
      if (cached && Date.now() - cached.fetchedAt <= ttlMs) {
        return cached.data;
      }
      controller?.abort();
      controller = new AbortController();
      const result = await fetcher(controller.signal);
      swrCache.set(currentKey, { data: result, fetchedAt: Date.now() });
      return result;
    },
    {
      initialValue: cacheEntry()?.data as T | undefined,
    },
  );

  onCleanup(() => controller?.abort());

  return {
    get data() {
      return resource() ?? cacheEntry()?.data;
    },
    get error() {
      const current = resource.error;
      return current instanceof Error ? current : current ? new Error(String(current)) : undefined;
    },
    get isLoading() {
      return resource.loading;
    },
    get isStale() {
      return isStale();
    },
    mutate() {
      swrCache.delete(resolvedKey());
      setRefreshToken((value) => value + 1);
      void refetch();
    },
  };
}
