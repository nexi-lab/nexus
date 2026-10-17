/**
 * Global application state: connection, navigation, config.
 */

import { createStore as create } from "./create-store.js";
import type { NativeIdentity, NexusClientOptions } from "@nexus-ai-fs/api-client";
import { FetchClient, resolveConfig } from "@nexus-ai-fs/api-client";
import { categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";

// ─── Client factory ───────────────────────────────────────────────────────────

/**
 * Factory that creates (or returns) the HTTP client for a given config.
 * Defaults to constructing a direct FetchClient (used in tests and SSR contexts).
 * Replaced at startup by the WorkerManager to route calls through the worker thread.
 *
 * @see §2 Worker thread isolation — Issue #3632
 */
let _clientFactory: (config: NexusClientOptions) => FetchClient = (config) =>
  new FetchClient(config);
let connectionAttempt = 0;

/**
 * Override the HTTP client factory. Call this once before initConfig(), passing
 * a factory that returns a WorkerFetchClient backed by the WorkerManager.
 */
export function setClientFactory(factory: (config: NexusClientOptions) => FetchClient): void {
  _clientFactory = factory;
}

export type ConnectionStatus = "disconnected" | "connecting" | "connected" | "error";

export type PanelId =
  | "files"
  | "versions"
  | "agents"
  | "zones"
  | "access"
  | "payments"
  | "search"
  | "workflows"
  | "infrastructure"
  | "console"
  | "connectors"
  | "stack";

/** Response from GET /api/v2/features */
export interface FeaturesResponse {
  readonly profile: string;
  readonly mode: string;
  readonly enabled_bricks: readonly string[];
  readonly disabled_bricks: readonly string[];
  readonly version: string | null;
  readonly rate_limit_enabled: boolean;
}

export interface GlobalState {
  // Connection
  readonly connectionStatus: ConnectionStatus;
  readonly connectionError: string | null;
  readonly config: NexusClientOptions;
  readonly client: FetchClient | null;

  // Navigation
  readonly activePanel: PanelId;
  readonly panelHistory: readonly PanelId[];

  // Identity display
  readonly serverVersion: string | null;
  readonly zoneId: string | null;
  readonly uptime: number | null;
  readonly userInfo: NativeIdentity | null;

  // Features (from GET /api/v2/features)
  readonly enabledBricks: readonly string[];
  readonly profile: string | null;
  readonly mode: string | null;
  readonly featuresLoaded: boolean;
  readonly featuresLastFetched: number;

  // Actions
  readonly initConfig: (overrides?: Partial<NexusClientOptions>) => void;
  readonly testConnection: () => Promise<void>;
  readonly setActivePanel: (panel: PanelId) => void;
  readonly setConnectionStatus: (status: ConnectionStatus, error?: string) => void;
  readonly setServerInfo: (info: { version?: string; zoneId?: string; uptime?: number }) => void;
  readonly setIdentity: (identity: { agentId?: string; subject?: string; zoneId?: string }) => void;
  readonly setFeatures: (features: FeaturesResponse) => void;
  readonly refreshFeatures: () => Promise<void>;
}

export const useGlobalStore = create<GlobalState>((set, get) => ({
  // Initial state
  connectionStatus: "disconnected",
  connectionError: null,
  config: resolveConfig(),
  client: null,
  activePanel: "files",
  panelHistory: [],
  serverVersion: null,
  zoneId: null,
  uptime: null,
  userInfo: null,
  enabledBricks: [],
  profile: null,
  mode: null,
  featuresLoaded: false,
  featuresLastFetched: 0,

  initConfig: (overrides) => {
    const config = resolveConfig({ transformKeys: false, ...overrides });
    const client = _clientFactory(config);
    set({
      config, client, userInfo: null, connectionStatus: client ? "connecting" : "disconnected",
      serverVersion: null, uptime: null, zoneId: null, enabledBricks: [],
      profile: null, mode: null, featuresLoaded: false, featuresLastFetched: 0,
    });

    if (client) {
      get().testConnection();
    }
  },

  testConnection: async () => {
    const { client, config } = get();
    const attempt = ++connectionAttempt;
    const isCurrent = () => get().client === client && get().config === config && attempt === connectionAttempt;
    if (!client) {
      set({ connectionStatus: "disconnected", connectionError: null, userInfo: null });
      return;
    }

    set({ connectionStatus: "connecting", connectionError: null, userInfo: null });

    try {
      const health = await client.get<{ status: string; version: string }>(
        "/v2/status", { timeout: 8_000 },
      ).catch(() => null);
      if (!isCurrent()) return;

      if (health?.status !== "ok") {
        throw new Error("Server health check failed");
      }

      set({
        serverVersion: health.version,
      });

      // Optional feature metadata and identity do not delay connection readiness.
      get().setConnectionStatus("connected");

      void (async () => {
        try {
          const authClient = new FetchClient({ ...config, transformKeys: false, maxRetries: 0, timeout: 4_000 });
          const info = await authClient.get<NativeIdentity>("/v2/auth/whoami");
          if (info && isCurrent() && get().connectionStatus === "connected") {
            set({ userInfo: info });
          }
        } catch { /* Identity remains unavailable when the credential is rejected. */ }
      })();
    } catch (err) {
      if (!isCurrent()) return;
      const message = err instanceof Error ? err.message : "Connection test failed";
      set({
        connectionStatus: "error",
        connectionError: message,
        userInfo: null,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: "global" });
    }
  },

  setActivePanel: (panel) => {
    const current = get().activePanel;
    if (current === panel) return;
    set((state) => ({
      activePanel: panel,
      panelHistory: [...state.panelHistory.slice(-9), current],
    }));
  },

  setConnectionStatus: (status, error) => {
    if (status === "disconnected" || status === "error") ++connectionAttempt;
    const previous = get().connectionStatus;
    // Clear stale identity when leaving connected state so the status bar
    // never shows a previous principal during reconnect/disconnect flows.
    const clearIdentity = previous === "connected" && status !== "connected" ? { userInfo: null } : {};
    set({ connectionStatus: status, connectionError: error ?? null, ...clearIdentity });
    // Refresh features whenever a connection is (re-)established.
    if (status === "connected" && previous !== "connected") {
      void get().refreshFeatures();
    }
  },

  setServerInfo: (info) => {
    set({
      serverVersion: info.version ?? get().serverVersion,
      zoneId: info.zoneId ?? get().zoneId,
      uptime: info.uptime ?? get().uptime,
    });
  },

  setIdentity: (identity) => {
    const currentConfig = get().config;
    // Use explicit values from identity, allowing empty string → undefined to clear fields
    const config: NexusClientOptions = {
      ...currentConfig,
      agentId: "agentId" in identity ? identity.agentId : currentConfig.agentId,
      subject: "subject" in identity ? identity.subject : currentConfig.subject,
      zoneId: "zoneId" in identity ? identity.zoneId : currentConfig.zoneId,
    };
    // _clientFactory reconfigures the worker thread and returns the stable client.
    const client = _clientFactory(config);
    set({ config, client, userInfo: null });
  },

  setFeatures: (features) => {
    set({
      enabledBricks: features.enabled_bricks ?? [],
      profile: features.profile ?? null,
      mode: features.mode ?? null,
      featuresLoaded: true,
      featuresLastFetched: Date.now(),
    });
  },

  refreshFeatures: async () => {
    const { client, config, featuresLastFetched } = get();
    if (!client) return;
    // TTL: skip if fetched within the last 30 seconds (Decision 13A — bumped from 10s;
    // now a reconnect-storm guard rather than a panel-switch guard)
    if (Date.now() - featuresLastFetched < 30_000) return;
    try {
      const features = await client.get<FeaturesResponse>("/api/v2/features");
      if (get().client === client && get().config === config) get().setFeatures(features);
    } catch {
      // Non-critical: use last known state
    }
  },
}));
