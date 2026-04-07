/**
 * Zustand store for Infrastructure panel: connectors, subscriptions, locks, secrets audit.
 *
 * Complements the events-store (SSE streaming) with REST-based infra management.
 */

import { createStore as create } from "./create-store.js";
import type { FetchClient } from "@nexus-ai-fs/api-client";
import { createApiAction, categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";
import { useUiStore } from "./ui-store.js";

// =============================================================================
// Types (snake_case matching API wire format)
// =============================================================================

export interface Connector {
  readonly connector_id: string;
  readonly name: string;
  readonly type: string;
  readonly status: "active" | "inactive" | "error";
  readonly capabilities: readonly string[];
  readonly config: Record<string, unknown>;
  readonly created_at: string;
  readonly last_seen: string | null;
}

export interface Subscription {
  readonly subscription_id: string;
  readonly event_type: string;
  readonly endpoint: string;
  readonly status: "active" | "paused" | "failed";
  readonly filter: string | null;
  readonly created_at: string;
  readonly last_triggered: string | null;
  readonly trigger_count: number;
}

/** Lock shape matching backend LockInfoMutex / LockInfoSemaphore. */
export interface Lock {
  readonly lock_id: string;
  readonly mode: "mutex" | "semaphore";
  readonly max_holders: number;
  readonly holder_info: string;
  readonly acquired_at: number;
  readonly expires_at: number;
  readonly fence_token: number;
  /** The resource path this lock is held on (derived from the list key). */
  readonly resource: string;
}

/** Matches backend SecretAuditEventResponse. */
export interface SecretAuditEntry {
  readonly id: string;
  readonly record_hash: string;
  readonly created_at: string;
  readonly event_type: string;
  readonly actor_id: string;
  readonly provider: string | null;
  readonly credential_id: string | null;
  readonly token_family_id: string | null;
  readonly zone_id: string;
  readonly ip_address: string | null;
  readonly details: string | null;
  readonly metadata_hash: string | null;
}

export interface OperationItem {
  readonly operation_id: string;
  readonly agent_id: string | null;
  readonly type: string;
  readonly status: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;
}

/** Audit transaction from /api/v2/audit/transactions. */
export interface AuditTransaction {
  readonly transaction_id: string;
  readonly actor_id: string;
  readonly action: string;
  readonly resource: string;
  readonly timestamp: string;
  readonly status: string;
  readonly details: string | null;
}

export type InfraTab = "connectors" | "subscriptions" | "locks" | "secrets";
export type EventsPanelTab = "events" | "mcl" | "replay" | "operations" | "audit" | InfraTab;

// =============================================================================
// Store
// =============================================================================

export interface InfraState {
  // Connectors
  readonly connectors: readonly Connector[];
  readonly selectedConnectorIndex: number;
  readonly connectorsLoading: boolean;

  // Subscriptions
  readonly subscriptions: readonly Subscription[];
  readonly selectedSubscriptionIndex: number;
  readonly subscriptionsLoading: boolean;

  // Locks
  readonly locks: readonly Lock[];
  readonly selectedLockIndex: number;
  readonly locksLoading: boolean;

  // Secrets audit
  readonly secretAuditEntries: readonly SecretAuditEntry[];
  readonly secretsLoading: boolean;

  // Operations
  readonly operations: readonly OperationItem[];
  readonly operationsLoading: boolean;
  readonly selectedOperationIndex: number;

  // Connector capabilities
  readonly connectorCapabilities: unknown | null;
  readonly capabilitiesLoading: boolean;

  // Audit transactions
  readonly auditTransactions: readonly AuditTransaction[];
  readonly auditLoading: boolean;
  readonly auditHasMore: boolean;
  readonly auditNextCursor: string | null;

  // Navigation
  readonly activeTab: InfraTab;
  readonly activePanelTab: EventsPanelTab;

  // Error
  readonly error: string | null;

  // Actions
  readonly fetchConnectors: (client: FetchClient) => Promise<void>;
  readonly fetchSubscriptions: (client: FetchClient) => Promise<void>;
  readonly createSubscription: (
    eventType: string,
    endpoint: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly deleteSubscription: (id: string, client: FetchClient) => Promise<void>;
  readonly testSubscription: (id: string, client: FetchClient) => Promise<void>;
  readonly fetchLocks: (client: FetchClient) => Promise<void>;
  readonly acquireLock: (path: string, mode: "mutex" | "semaphore", ttlSeconds: number, client: FetchClient) => Promise<void>;
  readonly releaseLock: (path: string, lockId: string, client: FetchClient) => Promise<void>;
  readonly extendLock: (path: string, lockId: string, ttlSeconds: number, client: FetchClient) => Promise<void>;
  readonly fetchSecretAudit: (client: FetchClient) => Promise<void>;
  readonly fetchOperations: (client: FetchClient) => Promise<void>;
  readonly fetchConnectorCapabilities: (connectorName: string, client: FetchClient) => Promise<void>;
  readonly fetchAuditTransactions: (filters: { cursor?: string; limit?: number }, client: FetchClient) => Promise<void>;
  readonly setActiveTab: (tab: InfraTab) => void;
  readonly setActivePanelTab: (tab: EventsPanelTab) => void;
  readonly setSelectedOperationIndex: (index: number) => void;
  readonly setSelectedConnectorIndex: (index: number) => void;
  readonly setSelectedSubscriptionIndex: (index: number) => void;
  readonly setSelectedLockIndex: (index: number) => void;
}

const SOURCE = "infrastructure";

export const useInfraStore = create<InfraState>((set, get) => ({
  connectors: [],
  selectedConnectorIndex: 0,
  connectorsLoading: false,
  subscriptions: [],
  selectedSubscriptionIndex: 0,
  subscriptionsLoading: false,
  locks: [],
  selectedLockIndex: 0,
  locksLoading: false,
  secretAuditEntries: [],
  secretsLoading: false,
  operations: [],
  operationsLoading: false,
  selectedOperationIndex: 0,
  connectorCapabilities: null,
  capabilitiesLoading: false,
  auditTransactions: [],
  auditLoading: false,
  auditHasMore: false,
  auditNextCursor: null,
  activeTab: "connectors",
  activePanelTab: "events",
  error: null,

  // =========================================================================
  // Actions with loading keys — createApiAction
  // =========================================================================

  fetchConnectors: createApiAction<InfraState, [FetchClient]>(set, {
    loadingKey: "connectorsLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch connectors",
    action: async (client) => {
      const response = await client.get<{
        readonly connectors: readonly any[];
      }>("/api/v2/connectors");
      // Normalize: API returns category/name, store expects type/connector_id/status
      const connectors: Connector[] = (response.connectors ?? []).map((c: any) => ({
        connector_id: c.connector_id ?? c.name ?? "",
        name: c.name ?? "",
        type: c.type ?? c.category ?? "",
        status: c.status ?? "active",
        capabilities: c.capabilities ?? [],
        config: c.config ?? {},
        created_at: c.created_at ?? "",
        last_seen: c.last_seen ?? null,
      }));
      return { connectors };
    },
  }),

  fetchSubscriptions: createApiAction<InfraState, [FetchClient]>(set, {
    loadingKey: "subscriptionsLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch subscriptions",
    action: async (client) => {
      const response = await client.get<{
        readonly subscriptions: readonly Subscription[];
      }>("/api/v2/subscriptions");
      return {
        subscriptions: response.subscriptions ?? [],
        selectedSubscriptionIndex: 0,
      };
    },
  }),

  fetchLocks: createApiAction<InfraState, [FetchClient]>(set, {
    loadingKey: "locksLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch locks",
    action: async (client) => {
      const response = await client.get<{
        readonly locks: readonly Lock[];
        readonly count: number;
      }>("/api/v2/locks");
      return { locks: response.locks ?? [], selectedLockIndex: 0 };
    },
  }),

  fetchSecretAudit: createApiAction<InfraState, [FetchClient]>(set, {
    loadingKey: "secretsLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch secrets audit",
    action: async (client) => {
      const response = await client.get<{
        readonly events: readonly SecretAuditEntry[];
        readonly limit: number;
        readonly has_more: boolean;
        readonly total: number | null;
        readonly next_cursor: string | null;
      }>("/api/v2/secrets-audit/events");
      return { secretAuditEntries: response.events ?? [] };
    },
  }),

  fetchOperations: createApiAction<InfraState, [FetchClient]>(set, {
    loadingKey: "operationsLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch operations",
    action: async (client) => {
      const response = await client.get<{
        readonly operations: readonly any[];
      }>("/api/v2/operations?limit=20");
      // Normalize API fields (id → operation_id, operation_type → type)
      const ops: OperationItem[] = (response.operations ?? []).map((op: any) => ({
        operation_id: op.operation_id ?? op.id ?? "",
        agent_id: op.agent_id ?? null,
        type: op.type ?? op.operation_type ?? "",
        status: op.status ?? "",
        started_at: op.started_at ?? op.timestamp ?? null,
        completed_at: op.completed_at ?? null,
      }));
      return {
        operations: ops,
        selectedOperationIndex: 0,
      };
    },
  }),

  fetchConnectorCapabilities: createApiAction<InfraState, [string, FetchClient]>(set, {
    loadingKey: "capabilitiesLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch connector capabilities",
    action: async (connectorName, client) => {
      const response = await client.get<{
        readonly capabilities: unknown;
      }>(`/api/v2/connectors/${encodeURIComponent(connectorName)}/capabilities`);
      return { connectorCapabilities: response.capabilities ?? null };
    },
  }),

  fetchAuditTransactions: async (filters, client) => {
    set({ auditLoading: true, error: null });
    try {
      const params = new URLSearchParams();
      if (filters.cursor) params.set("cursor", filters.cursor);
      params.set("limit", String(filters.limit ?? 50));
      const qs = params.toString();
      const url = `/api/v2/audit/transactions${qs ? `?${qs}` : ""}`;
      const response = await client.get<{
        readonly transactions: readonly AuditTransaction[];
        readonly has_more: boolean;
        readonly next_cursor: string | null;
      }>(url);
      const incoming = (response.transactions ?? []).map((t) => ({
        transaction_id: t.transaction_id ?? "",
        actor_id: t.actor_id ?? "",
        action: t.action ?? "",
        resource: t.resource ?? "",
        timestamp: t.timestamp ?? "",
        status: t.status ?? "",
        details: t.details ?? null,
      }));
      set((state) => {
        const combined = filters.cursor
          ? [...state.auditTransactions, ...incoming]
          : incoming;
        return {
          auditTransactions: combined.slice(-1000), // keep latest 1000
          auditLoading: false,
          auditHasMore: response.has_more ?? false,
          auditNextCursor: response.next_cursor ?? null,
        };
      });
      useUiStore.getState().markDataUpdated("infrastructure");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch audit transactions";
      set({ auditLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  // =========================================================================
  // Actions without loading keys — inline with error store integration
  // =========================================================================

  createSubscription: async (eventType, endpoint, client) => {
    set({ error: null });
    try {
      await client.post<Subscription>("/api/v2/subscriptions", {
        event_type: eventType,
        endpoint,
      });
      await get().fetchSubscriptions(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to create subscription";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  deleteSubscription: async (id, client) => {
    set({ error: null });
    try {
      await client.delete(`/api/v2/subscriptions/${encodeURIComponent(id)}`);
      set((state) => ({
        subscriptions: state.subscriptions.filter((s) => s.subscription_id !== id),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to delete subscription";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  testSubscription: async (id, client) => {
    set({ error: null });
    try {
      await client.post(
        `/api/v2/subscriptions/${encodeURIComponent(id)}/test`,
        {},
      );
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to test subscription";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  acquireLock: async (path, mode, ttlSeconds, client) => {
    set({ error: null });
    try {
      await client.post(`/api/v2/locks/${encodeURIComponent(path)}/acquire`, {
        mode,
        ttl_seconds: ttlSeconds,
      });
      // Refresh lock list after acquisition
      await get().fetchLocks(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to acquire lock";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  releaseLock: async (path, lockId, client) => {
    set({ error: null });
    try {
      await client.deleteNoContent(
        `/api/v2/locks/${encodeURIComponent(path)}?lock_id=${encodeURIComponent(lockId)}`,
      );
      set((state) => ({
        locks: state.locks.filter((l) => l.lock_id !== lockId),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to release lock";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  extendLock: async (path, lockId, ttlSeconds, client) => {
    set({ error: null });
    try {
      await client.patch(`/api/v2/locks/${encodeURIComponent(path)}`, {
        lock_id: lockId,
        ttl: ttlSeconds,
      });
      await get().fetchLocks(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to extend lock";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  setActiveTab: (tab) => {
    set({ activeTab: tab, activePanelTab: tab, error: null });
  },

  setActivePanelTab: (tab) => {
    set((state) => ({
      activePanelTab: tab,
      activeTab: tab === "events" || tab === "mcl" || tab === "replay" || tab === "operations" || tab === "audit"
        ? state.activeTab
        : tab,
      error: null,
    }));
  },

  setSelectedOperationIndex: (index) => {
    set({ selectedOperationIndex: index });
  },

  setSelectedConnectorIndex: (index) => {
    set({ selectedConnectorIndex: index });
  },

  setSelectedSubscriptionIndex: (index) => {
    set({ selectedSubscriptionIndex: index });
  },

  setSelectedLockIndex: (index) => {
    set({ selectedLockIndex: index });
  },
}));

// =============================================================================
// SSE bus handler — live infra updates (#3632 §3)
// =============================================================================

import { useSseBus } from "./sse-bus.js";
import { useGlobalStore } from "./global-store.js";

const INFRA_EVENT_TYPES = new Set(["mount", "unmount"]);

useSseBus.getState().registerHandler("infra", (events) => {
  const relevant = events.some((ev) => INFRA_EVENT_TYPES.has(ev.type));
  if (!relevant) return;

  queueMicrotask(() => {
    const client = useGlobalStore.getState().client;
    if (client) {
      void useInfraStore.getState().fetchConnectors(client);
    }
    useUiStore.getState().markDataUpdated("infrastructure");
  });
}, { debounceMs: 1000 });
