/**
 * Zustand store for Infrastructure panel: connectors, subscriptions, locks, secrets audit.
 *
 * Complements the events-store (SSE streaming) with REST-based infra management.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

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

export interface Lock {
  readonly lock_id: string;
  readonly resource: string;
  readonly holder: string;
  readonly status: "held" | "released" | "expired";
  readonly acquired_at: string;
  readonly expires_at: string;
  readonly ttl_seconds: number;
}

export interface SecretAuditEntry {
  readonly entry_id: string;
  readonly action: string;
  readonly secret_name: string;
  readonly actor: string;
  readonly timestamp: string;
  readonly ip_address: string | null;
  readonly result: "success" | "denied" | "error";
}

export type InfraTab = "connectors" | "subscriptions" | "locks" | "secrets";

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

  // Navigation
  readonly activeTab: InfraTab;

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
  readonly releaseLock: (id: string, client: FetchClient) => Promise<void>;
  readonly extendLock: (id: string, ttlSeconds: number, client: FetchClient) => Promise<void>;
  readonly fetchSecretAudit: (client: FetchClient) => Promise<void>;
  readonly setActiveTab: (tab: InfraTab) => void;
  readonly setSelectedConnectorIndex: (index: number) => void;
  readonly setSelectedSubscriptionIndex: (index: number) => void;
  readonly setSelectedLockIndex: (index: number) => void;
}

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
  activeTab: "connectors",
  error: null,

  fetchConnectors: async (client) => {
    set({ connectorsLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly connectors: readonly Connector[];
      }>("/api/v2/connectors");
      set({ connectors: response.connectors ?? [], connectorsLoading: false });
    } catch (err) {
      set({
        connectorsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch connectors",
      });
    }
  },

  fetchSubscriptions: async (client) => {
    set({ subscriptionsLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly subscriptions: readonly Subscription[];
      }>("/api/v2/subscriptions");
      set({
        subscriptions: response.subscriptions ?? [],
        subscriptionsLoading: false,
        selectedSubscriptionIndex: 0,
      });
    } catch (err) {
      set({
        subscriptionsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch subscriptions",
      });
    }
  },

  createSubscription: async (eventType, endpoint, client) => {
    set({ error: null });
    try {
      await client.post<Subscription>("/api/v2/subscriptions", {
        event_type: eventType,
        endpoint,
      });
      await get().fetchSubscriptions(client);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to create subscription",
      });
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
      set({
        error: err instanceof Error ? err.message : "Failed to delete subscription",
      });
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
      set({
        error: err instanceof Error ? err.message : "Failed to test subscription",
      });
    }
  },

  fetchLocks: async (client) => {
    set({ locksLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly locks: readonly Lock[];
      }>("/api/v2/locks");
      set({ locks: response.locks ?? [], locksLoading: false, selectedLockIndex: 0 });
    } catch (err) {
      set({
        locksLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch locks",
      });
    }
  },

  releaseLock: async (id, client) => {
    set({ error: null });
    try {
      await client.post(`/api/v2/locks/${encodeURIComponent(id)}/release`, {});
      set((state) => ({
        locks: state.locks.map((l) =>
          l.lock_id === id ? { ...l, status: "released" as const } : l,
        ),
      }));
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to release lock",
      });
    }
  },

  extendLock: async (id, ttlSeconds, client) => {
    set({ error: null });
    try {
      await client.post(`/api/v2/locks/${encodeURIComponent(id)}/extend`, {
        ttl_seconds: ttlSeconds,
      });
      await get().fetchLocks(client);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to extend lock",
      });
    }
  },

  fetchSecretAudit: async (client) => {
    set({ secretsLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly entries: readonly SecretAuditEntry[];
      }>("/api/v2/secrets/audit");
      set({ secretAuditEntries: response.entries ?? [], secretsLoading: false });
    } catch (err) {
      set({
        secretsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch secrets audit",
      });
    }
  },

  setActiveTab: (tab) => {
    set({ activeTab: tab, error: null });
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
