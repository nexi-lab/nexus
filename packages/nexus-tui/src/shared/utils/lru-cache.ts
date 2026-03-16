/**
 * Generic LRU cache with O(n) eviction.
 *
 * Uses a monotonic access counter for LRU ordering.
 * Eviction finds the minimum-access-order entry in a single pass (O(n))
 * instead of sorting (O(n log n)).
 *
 * @see Issue #3102, Decisions 7A + 16A
 */

interface CacheEntry<T> {
  data: T;
  fetchedAt: number;
}

let accessCounter = 0;

export class LruCache<T = unknown> {
  private readonly _map = new Map<string, { entry: CacheEntry<T>; accessOrder: number }>();
  private readonly _maxSize: number;

  constructor(maxSize: number = 200) {
    this._maxSize = maxSize;
  }

  get(key: string): CacheEntry<T> | undefined {
    const item = this._map.get(key);
    if (!item) return undefined;
    // Update access order for LRU tracking (monotonic counter avoids Date.now() ties)
    item.accessOrder = ++accessCounter;
    return item.entry;
  }

  set(key: string, entry: CacheEntry<T>): void {
    this._map.set(key, { entry, accessOrder: ++accessCounter });
    this._evictIfNeeded();
  }

  has(key: string): boolean {
    return this._map.has(key);
  }

  delete(key: string): void {
    this._map.delete(key);
  }

  clear(): void {
    this._map.clear();
  }

  get size(): number {
    return this._map.size;
  }

  private _evictIfNeeded(): void {
    while (this._map.size > this._maxSize) {
      // O(n) min-scan: find the least-recently-accessed entry
      let minKey: string | undefined;
      let minOrder = Infinity;

      for (const [key, item] of this._map) {
        if (item.accessOrder < minOrder) {
          minOrder = item.accessOrder;
          minKey = key;
        }
      }

      if (minKey !== undefined) {
        this._map.delete(minKey);
      } else {
        break;
      }
    }
  }
}
