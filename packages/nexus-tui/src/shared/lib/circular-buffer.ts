/**
 * Fixed-capacity circular buffer for bounded collections.
 *
 * Primary use: SSE event stream (prevents unbounded memory growth).
 * When the buffer is full, the oldest item is silently evicted.
 *
 * @see Issue #3066, Phase D9 (SSE stream bounding)
 */

export class CircularBuffer<T> {
  private readonly _items: Array<T | undefined>;
  private _head = 0; // next write position
  private _size = 0;
  private _totalAdded = 0;

  constructor(readonly capacity: number) {
    if (capacity < 1) throw new Error("CircularBuffer capacity must be >= 1");
    this._items = new Array<T | undefined>(capacity);
  }

  /** Number of items currently in the buffer. */
  get size(): number {
    return this._size;
  }

  /** Total items ever added (including evicted). */
  get totalAdded(): number {
    return this._totalAdded;
  }

  /** Number of items that have been evicted. */
  get evictedCount(): number {
    return this._totalAdded - this._size;
  }

  /** Whether the buffer has evicted any items. */
  get hasOverflowed(): boolean {
    return this._totalAdded > this.capacity;
  }

  /** Add an item. If full, the oldest item is evicted. */
  push(item: T): void {
    this._items[this._head] = item;
    this._head = (this._head + 1) % this.capacity;
    this._totalAdded++;
    if (this._size < this.capacity) {
      this._size++;
    }
  }

  /** Get item by index (0 = oldest). Throws if out of range. */
  get(index: number): T {
    if (index < 0 || index >= this._size) {
      throw new RangeError(`Index ${index} out of range [0, ${this._size})`);
    }
    const start = this._size < this.capacity
      ? 0
      : this._head; // head points to oldest when full
    const actual = (start + index) % this.capacity;
    return this._items[actual] as T;
  }

  /** Remove all items and reset counters. */
  clear(): void {
    this._items.fill(undefined);
    this._head = 0;
    this._size = 0;
    this._totalAdded = 0;
  }

  /** Iterate from oldest to newest. */
  *[Symbol.iterator](): Iterator<T> {
    for (let i = 0; i < this._size; i++) {
      yield this.get(i);
    }
  }

  /** Return all items as an array (oldest first). */
  toArray(): T[] {
    return Array.from(this);
  }
}
