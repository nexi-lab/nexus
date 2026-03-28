/**
 * Global application state: connection, navigation, config.
 */

import { create } from "zustand";
import type { NexusClientOptions } from "@nexus/api-client";
import { FetchClient, resolveConfig } from "@nexus/api-client";
import { categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";

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
  | "connectors";

/** Response from GET /api/v2/features */
export interface FeaturesResponse {
  readonly profile: string;
  readonly mode: string;
  readonly enabled_bricks: readonly string[];
  readonly disabled_bricks: readonly string[];
  readonly version: string | null;
  readonly rate_limit_enabled: boolean;
}

/** Response from GET /auth/me */
export interface UserInfo {
  readonly user_id: string;
  readonly email: string;
  readonly username: string | null;
  readonly display_name: string | null;
  readonly avatar_url: string | null;
  readonly is_global_admin: boolean;
  readonly primary_auth_method: string | null;
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
  readonly userInfo: UserInfo | null;

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
    const client = config.apiKey ? new FetchClient(config) : null;
    set({ config, client, connectionStatus: client ? "connecting" : "disconnected" });

    if (client) {
      get().testConnection();
    }
  },

  testConnection: async () => {
    const client = get().client;
    if (!client) {
      set({ connectionStatus: "disconnected", connectionError: null, userInfo: null });
      return;
    }

    set({ connectionStatus: "connecting", connectionError: null });

    try {
      // Consolidated connection check (Decision 5A): health + features + auth in one flow
      let [health, features, userInfo] = await Promise.all([
        client.get<{ status?: string; uptime_seconds?: number }>(
          "/healthz/ready",
        ).catch(() => null),
        client.get<FeaturesResponse>("/api/v2/features").catch(() => null),
        client.get<UserInfo>("/auth/me").catch(() => null),
      ]);

      // Auto-discovery: if health check fails, scan common ports to find the server.
      // Only probe when running interactively (not in tests) — probing creates real
      // HTTP connections that cause test timeouts.
      if (!health && typeof process !== "undefined" && process.stdout?.isTTY) {
        const configuredUrl = get().config.baseUrl ?? "";
        const hostname = configuredUrl.replace(/:\d+$/, "").replace(/^https?:\/\//, "");
        const protocol = configuredUrl.startsWith("https") ? "https" : "http";
        const PROBE_PORTS = [2026, 2027, 2042, 2043, 8080, 2122];

        for (const port of PROBE_PORTS) {
          if (configuredUrl.includes(`:${port}`)) continue;
          try {
            const probeUrl = `${protocol}://${hostname || "localhost"}:${port}`;
            const probeClient = new FetchClient({ ...get().config, baseUrl: probeUrl, timeout: 3000, maxRetries: 0 });
            const probeHealth = await probeClient.get<{ status?: string; uptime_seconds?: number }>(
              "/healthz/ready",
            ).catch(() => null);
            if (probeHealth) {
              const newConfig = resolveConfig({ transformKeys: false, baseUrl: probeUrl });
              const newClient = new FetchClient(newConfig);
              [health, features, userInfo] = await Promise.all([
                Promise.resolve(probeHealth),
                newClient.get<FeaturesResponse>("/api/v2/features").catch(() => null),
                newClient.get<UserInfo>("/auth/me").catch(() => null),
              ]);
              set({ config: newConfig, client: newClient });
              break;
            }
          } catch {
            // probe failed, try next port
          }
        }
      }

      if (!health) {
        throw new Error("Server health check failed");
      }

      // Note: zoneId is not set here — no health/readiness endpoint provides it.
      // The old /api/v2/bricks/health never returned zone_id either (its response
      // was { total, active, failed, bricks }). zoneId is set via setIdentity()
      // when the user explicitly configures a zone, or defaults to "root".
      set({
        serverVersion: features?.version ?? get().serverVersion,
        uptime: health.uptime_seconds ?? get().uptime,
      });
      if (features) {
        get().setFeatures(features);
      }

      set({ connectionStatus: "connected", connectionError: null, userInfo });
    } catch (err) {
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
    // Re-fetch features on panel switch (Decision 3A)
    get().refreshFeatures();
  },

  setConnectionStatus: (status, error) => {
    set({ connectionStatus: status, connectionError: error ?? null });
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
    const client = config.apiKey ? new FetchClient(config) : null;
    set({ config, client });
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
    const { client, featuresLastFetched } = get();
    if (!client) return;
    // TTL: skip if fetched within the last 10 seconds (Decision 13A)
    if (Date.now() - featuresLastFetched < 10_000) return;
    try {
      const features = await client.get<{
        profile: string;
        mode: string;
        enabled_bricks: string[];
        disabled_bricks: string[];
        version: string | null;
        rate_limit_enabled: boolean;
      }>("/api/v2/features");
      get().setFeatures(features);
    } catch {
      // Non-critical: use last known state
    }
  },
}));
