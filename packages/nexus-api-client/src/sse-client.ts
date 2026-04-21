/**
 * SSE connection manager with ring buffer, throttled flush,
 * and exponential backoff reconnection.
 */

import type { SseEvent } from "./types.js";

const DEFAULT_BUFFER_CAPACITY = 1000;
const DEFAULT_FLUSH_INTERVAL_MS = 100;
const INITIAL_RECONNECT_DELAY_MS = 500;
const MAX_RECONNECT_DELAY_MS = 30_000;

/** Fixed-size circular buffer that drops oldest entries when full. */
export class RingBuffer<T> {
  private readonly buffer: (T | undefined)[];
  private head = 0;
  private count = 0;
  /** Monotonically increasing counter of total items ever pushed (never wraps). */
  private _totalPushed = 0;

  constructor(readonly capacity: number) {
    if (!Number.isInteger(capacity) || capacity < 1) {
      throw new RangeError("RingBuffer capacity must be a positive integer");
    }
    this.buffer = new Array<T | undefined>(capacity);
  }

  get size(): number {
    return this.count;
  }

  /** Total number of items pushed since creation/clear (never decreases). */
  get totalPushed(): number {
    return this._totalPushed;
  }

  push(item: T): void {
    this.buffer[this.head] = item;
    this.head = (this.head + 1) % this.capacity;
    if (this.count < this.capacity) {
      this.count++;
    }
    this._totalPushed++;
  }

  /** Return items in insertion order (oldest first). */
  toArray(): readonly T[] {
    if (this.count === 0) return [];

    const result: T[] = [];
    const start = this.count < this.capacity ? 0 : this.head;
    for (let i = 0; i < this.count; i++) {
      const index = (start + i) % this.capacity;
      result.push(this.buffer[index] as T);
    }
    return result;
  }

  /** Return the last N items (newest first becomes oldest first). */
  lastN(n: number): readonly T[] {
    if (n <= 0 || this.count === 0) return [];
    const take = Math.min(n, this.count);
    const result = new Array<T>(take);
    const start = (this.head - take + this.capacity) % this.capacity;
    for (let i = 0; i < take; i++) {
      const index = (start + i) % this.capacity;
      result[i] = this.buffer[index] as T;
    }
    return result;
  }

  clear(): void {
    this.buffer.fill(undefined);
    this.head = 0;
    this.count = 0;
    this._totalPushed = 0;
  }
}

export interface SseClientOptions {
  readonly baseUrl: string;
  readonly apiKey: string;
  readonly bufferCapacity?: number;
  readonly flushIntervalMs?: number;
  readonly fetch?: typeof globalThis.fetch;
  /** Agent identity sent as X-Agent-ID header. */
  readonly agentId?: string;
  /** Subject identity sent as X-Nexus-Subject header. */
  readonly subject?: string;
  /** Zone ID sent as X-Nexus-Zone-ID header. */
  readonly zoneId?: string;
}

export type SseEventHandler = (events: readonly SseEvent[]) => void;
export type SseErrorHandler = (error: Error) => void;
export type SseReconnectHandler = (attempt: number) => void;

export class SseClient {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly fetchFn: typeof globalThis.fetch;
  private readonly buffer: RingBuffer<SseEvent>;
  private readonly flushIntervalMs: number;
  private readonly agentId: string | undefined;
  private readonly subject: string | undefined;
  private readonly zoneId: string | undefined;

  private abortController: AbortController | null = null;
  private flushTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectAttempt = 0;
  private lastEventId: string | undefined;
  private connected = false;

  private eventHandler: SseEventHandler | null = null;
  private errorHandler: SseErrorHandler | null = null;
  private reconnectHandler: SseReconnectHandler | null = null;

  constructor(options: SseClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.fetchFn = options.fetch ?? globalThis.fetch;
    this.buffer = new RingBuffer(options.bufferCapacity ?? DEFAULT_BUFFER_CAPACITY);
    this.flushIntervalMs = options.flushIntervalMs ?? DEFAULT_FLUSH_INTERVAL_MS;
    this.agentId = options.agentId;
    this.subject = options.subject;
    this.zoneId = options.zoneId;
  }

  onEvent(handler: SseEventHandler): void {
    this.eventHandler = handler;
  }

  onError(handler: SseErrorHandler): void {
    this.errorHandler = handler;
  }

  onReconnect(handler: SseReconnectHandler): void {
    this.reconnectHandler = handler;
  }

  get isConnected(): boolean {
    return this.connected;
  }

  async connect(path: string): Promise<void> {
    this.disconnect();

    this.abortController = new AbortController();
    this.startFlushTimer();

    await this.connectWithRetry(path);
  }

  disconnect(): void {
    this.abortController?.abort();
    this.abortController = null;
    this.stopFlushTimer();
    this.connected = false;
    this.reconnectAttempt = 0;
    this.lastFlushedTotal = 0;
  }

  getBufferedEvents(): readonly SseEvent[] {
    return this.buffer.toArray();
  }

  clearBuffer(): void {
    this.buffer.clear();
    this.lastFlushedTotal = 0;
  }

  // ===========================================================================
  // Internal
  // ===========================================================================

  private async connectWithRetry(path: string): Promise<void> {
    while (this.abortController && !this.abortController.signal.aborted) {
      try {
        await this.streamEvents(path);
        if (this.abortController?.signal.aborted) return;

        // A clean stream close still means we should reconnect. Apply the
        // same backoff policy to avoid hot-loop reconnect storms.
        this.reconnectAttempt++;
        this.reconnectHandler?.(this.reconnectAttempt);
        await sleep(this.computeReconnectDelay());
      } catch (error) {
        if (this.abortController?.signal.aborted) return;

        this.connected = false;
        this.reconnectAttempt++;
        this.errorHandler?.(error instanceof Error ? error : new Error(String(error)));
        this.reconnectHandler?.(this.reconnectAttempt);

        const delay = this.computeReconnectDelay();
        await sleep(delay);
      }
    }
  }

  private async streamEvents(path: string): Promise<void> {
    const url = `${this.baseUrl}${path}`;
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`,
      Accept: "text/event-stream",
    };

    if (this.agentId) headers["X-Agent-ID"] = this.agentId;
    if (this.subject) headers["X-Nexus-Subject"] = this.subject;
    if (this.zoneId) headers["X-Nexus-Zone-ID"] = this.zoneId;

    if (this.lastEventId) {
      headers["Last-Event-ID"] = this.lastEventId;
    }

    const response = await this.fetchFn(url, {
      headers,
      signal: this.abortController?.signal,
    });

    if (!response.ok) {
      throw new Error(`SSE connection failed: HTTP ${response.status}`);
    }

    if (!response.body) {
      throw new Error("SSE response has no body");
    }

    this.connected = true;
    this.reconnectAttempt = 0;

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let partial = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        partial += decoder.decode(value, { stream: true });
        const events = this.parseEvents(partial);
        partial = events.remaining;

        for (const event of events.parsed) {
          this.buffer.push(event);
          if (event.id) {
            this.lastEventId = event.id;
          }
        }
      }
    } finally {
      reader.releaseLock();
      this.connected = false;
    }
  }

  private parseEvents(text: string): {
    parsed: SseEvent[];
    remaining: string;
  } {
    const parsed: SseEvent[] = [];
    const normalizedText = text.replace(/\r\n/g, "\n");
    const blocks = normalizedText.split("\n\n");

    // Last block may be incomplete — keep it as remaining
    const remaining = blocks.pop() ?? "";

    for (const block of blocks) {
      if (!block.trim()) continue;

      let id: string | undefined;
      let event = "message";
      let data = "";
      let retry: number | undefined;

      for (const line of block.split("\n")) {
        if (line.startsWith("id:")) {
          id = line.slice(3).trim();
        } else if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          data += (data ? "\n" : "") + line.slice(5).trim();
        } else if (line.startsWith("retry:")) {
          const val = parseInt(line.slice(6).trim(), 10);
          if (!Number.isNaN(val)) retry = val;
        }
      }

      if (data || event !== "message") {
        parsed.push({ id, event, data, retry });
      }
    }

    return { parsed, remaining };
  }

  /** Tracks total events flushed via the monotonic totalPushed counter. */
  private lastFlushedTotal = 0;

  private startFlushTimer(): void {
    this.lastFlushedTotal = this.buffer.totalPushed;
    this.flushTimer = setInterval(() => {
      const currentTotal = this.buffer.totalPushed;
      if (currentTotal > this.lastFlushedTotal) {
        const newCount = currentTotal - this.lastFlushedTotal;
        const newEvents = this.buffer.lastN(newCount);
        this.lastFlushedTotal = currentTotal;
        if (newEvents.length > 0) {
          this.eventHandler?.(newEvents);
        }
      }
    }, this.flushIntervalMs);
  }

  private stopFlushTimer(): void {
    if (this.flushTimer) {
      clearInterval(this.flushTimer);
      this.flushTimer = null;
    }
  }

  private computeReconnectDelay(): number {
    const exponential = INITIAL_RECONNECT_DELAY_MS * Math.pow(2, this.reconnectAttempt - 1);
    const capped = Math.min(exponential, MAX_RECONNECT_DELAY_MS);
    return capped + Math.random() * capped * 0.1; // 10% jitter
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
