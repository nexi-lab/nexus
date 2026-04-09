/**
 * Centralized SSE event bus — single connection shared across all panels.
 *
 * Manages one SseClient to /api/v2/events/stream at the app level.
 * Domain stores register handlers to receive typed, parsed events with
 * per-handler debouncing (default 500ms) to batch rapid updates.
 *
 * @see Issue #3632 §3 — SSE event streaming for live panel updates
 */

import { createStore as create } from "./create-store.js";
import type { SseEvent } from "@nexus-ai-fs/api-client";
import { SseClient } from "@nexus-ai-fs/api-client";

const MAX_RECONNECT_ATTEMPTS = 10;
const DEFAULT_DEBOUNCE_MS = 500;

// =============================================================================
// Types
// =============================================================================

export interface SseIdentity {
  readonly agentId?: string;
  readonly subject?: string;
  readonly zoneId?: string;
}

/** Parsed event extracted from SSE JSON data payload. */
export interface ParsedEvent {
  /** Original raw SSE event. */
  readonly raw: SseEvent;
  /** Event type parsed from JSON (e.g. "write", "delete", "mount"). */
  readonly type: string;
  /** File path if present in the event payload. */
  readonly path?: string;
  /** Zone ID if present. */
  readonly zoneId?: string;
  /** Agent ID if present. */
  readonly agentId?: string;
  /** Full parsed JSON payload. */
  readonly payload: Record<string, unknown>;
}

export type SseBusHandler = (events: readonly ParsedEvent[]) => void;

interface HandlerEntry {
  readonly handler: SseBusHandler;
  readonly debounceMs: number;
  pendingEvents: ParsedEvent[];
  timer: ReturnType<typeof setTimeout> | null;
}

export interface SseBusState {
  readonly connected: boolean;
  readonly reconnectCount: number;
  readonly reconnectExhausted: boolean;

  readonly connect: (baseUrl: string, apiKey: string, identity?: SseIdentity) => void;
  readonly disconnect: () => void;
  readonly reconnect: () => void;
  readonly registerHandler: (
    id: string,
    handler: SseBusHandler,
    opts?: { debounceMs?: number },
  ) => void;
  readonly unregisterHandler: (id: string) => void;
}

// =============================================================================
// Module-level state (not serializable, kept outside the store)
// =============================================================================

let sseClient: SseClient | null = null;
let lastConnectParams: { baseUrl: string; apiKey: string; identity?: SseIdentity } | null = null;
const handlers = new Map<string, HandlerEntry>();

/**
 * Connection generation counter. Incremented on every connect/disconnect.
 * Callbacks from retired connections check this before mutating state,
 * preventing stale events from leaking across identity switches.
 */
let connectionGen = 0;

/** Handle for the post-connect readiness check timer. */
let readinessTimer: ReturnType<typeof setTimeout> | null = null;

// =============================================================================
// Helpers
// =============================================================================

/** Parse a raw SSE event's JSON data into a ParsedEvent. Returns null on failure. */
function parseEvent(raw: SseEvent): ParsedEvent | null {
  if (!raw.data) return null;

  try {
    const payload = JSON.parse(raw.data) as Record<string, unknown>;
    return {
      raw,
      type: typeof payload.type === "string" ? payload.type : "unknown",
      path: typeof payload.path === "string" ? payload.path : undefined,
      zoneId: typeof payload.zone_id === "string" ? payload.zone_id : undefined,
      agentId: typeof payload.agent_id === "string" ? payload.agent_id : undefined,
      payload,
    };
  } catch {
    // Malformed JSON — still dispatch with raw data as-is
    return {
      raw,
      type: "unknown",
      payload: {},
    };
  }
}

/** Flush a handler's pending events. Isolated: exceptions don't propagate. */
function flushHandler(entry: HandlerEntry): void {
  if (entry.pendingEvents.length === 0) return;
  const batch = entry.pendingEvents;
  entry.pendingEvents = [];
  entry.timer = null;
  try {
    entry.handler(batch);
  } catch {
    // Handler threw — absorb so other handlers are not affected.
  }
}

/** Dispatch a parsed event to all registered handlers. */
function dispatch(parsed: ParsedEvent): void {
  for (const entry of handlers.values()) {
    entry.pendingEvents.push(parsed);

    if (entry.debounceMs <= 0) {
      // No debounce — flush immediately
      flushHandler(entry);
    } else if (entry.timer === null) {
      // Start debounce timer
      entry.timer = setTimeout(() => flushHandler(entry), entry.debounceMs);
    }
    // else: timer already running, event batched for next flush
  }
}

/** Clear all pending debounce timers (called on disconnect). */
function clearAllTimers(): void {
  if (readinessTimer !== null) {
    clearTimeout(readinessTimer);
    readinessTimer = null;
  }
  for (const entry of handlers.values()) {
    if (entry.timer !== null) {
      clearTimeout(entry.timer);
      entry.timer = null;
    }
    entry.pendingEvents = [];
  }
}

// =============================================================================
// Store
// =============================================================================

export const useSseBus = create<SseBusState>((set) => ({
  connected: false,
  reconnectCount: 0,
  reconnectExhausted: false,

  connect: (baseUrl, apiKey, identity) => {
    // Disconnect existing
    sseClient?.disconnect();
    clearAllTimers();

    // Bump generation so callbacks from the previous client become no-ops
    const gen = ++connectionGen;

    const client = new SseClient({
      baseUrl,
      apiKey,
      agentId: identity?.agentId,
      subject: identity?.subject,
      zoneId: identity?.zoneId,
    });

    client.onEvent((newEvents) => {
      queueMicrotask(() => {
        // Stale guard: ignore if a newer connect/disconnect has occurred
        if (gen !== connectionGen) return;

        // Mark connected on first successful event delivery
        set({ connected: true, reconnectCount: 0, reconnectExhausted: false });

        for (const raw of newEvents) {
          const parsed = parseEvent(raw);
          if (parsed) dispatch(parsed);
        }
      });
    });

    client.onReconnect((attempt) => {
      queueMicrotask(() => {
        if (gen !== connectionGen) return;
        if (attempt >= MAX_RECONNECT_ATTEMPTS) {
          client.disconnect();
          set({
            connected: false,
            reconnectExhausted: true,
            reconnectCount: attempt,
          });
          sseClient = null;
        } else {
          set({ connected: false, reconnectCount: attempt });
        }
      });
    });

    client.onError(() => {
      queueMicrotask(() => {
        if (gen !== connectionGen) return;
        set({ connected: false });
      });
    });

    sseClient = client;
    lastConnectParams = { baseUrl, apiKey, identity };
    set({
      connected: false,
      reconnectCount: 0,
      reconnectExhausted: false,
    });

    // Fire-and-forget connect
    client.connect("/api/v2/events/stream").catch(() => {
      queueMicrotask(() => {
        if (gen !== connectionGen) return;
        set({ connected: false });
      });
    });

    // Readiness check: if the underlying HTTP handshake completed before
    // any events arrive, mark connected for immediate UI feedback.
    readinessTimer = setTimeout(() => {
      readinessTimer = null;
      if (gen !== connectionGen) return;
      // Double-check that the active client is still the one we created
      if (sseClient === client && client.isConnected) {
        queueMicrotask(() => {
          if (gen !== connectionGen) return;
          set({ connected: true });
        });
      }
    }, 200);
  },

  disconnect: () => {
    connectionGen++;
    sseClient?.disconnect();
    sseClient = null;
    clearAllTimers();
    set({
      connected: false,
      reconnectCount: 0,
      reconnectExhausted: false,
    });
  },

  reconnect: () => {
    if (!lastConnectParams) return;
    const { baseUrl, apiKey, identity } = lastConnectParams;
    useSseBus.getState().connect(baseUrl, apiKey, identity);
  },

  registerHandler: (id, handler, opts) => {
    const existing = handlers.get(id);

    // Issue 8A: guard against accidental duplicate registrations.
    // In tests/dev: throw immediately so bugs surface at the call site.
    // In production: warn and overwrite (safe fallback — latest handler wins).
    if (existing) {
      const msg = `[sse-bus] Duplicate handler ID "${id}". ` +
        "Call unregisterHandler() before re-registering, or use a unique ID.";
      if (process.env.NODE_ENV !== "production") {
        throw new Error(msg);
      } else {
        console.warn(msg);
      }
      if (existing.timer != null) clearTimeout(existing.timer);
    }

    handlers.set(id, {
      handler,
      debounceMs: opts?.debounceMs ?? DEFAULT_DEBOUNCE_MS,
      pendingEvents: [],
      timer: null,
    });
  },

  unregisterHandler: (id) => {
    const entry = handlers.get(id);
    if (entry) {
      if (entry.timer !== null) clearTimeout(entry.timer);
      handlers.delete(id);
    }
  },
}));

/**
 * Expose internals for testing only.
 * @internal
 */
export const _testInternals = {
  get handlers() { return handlers; },
  get connectionGen() { return connectionGen; },
  parseEvent,
  dispatch,
  clearAllTimers,
};
