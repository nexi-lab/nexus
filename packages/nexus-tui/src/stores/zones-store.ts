/**
 * Zustand store for Zones & Federation panel.
 *
 * Manages zone listing, brick health, individual brick detail,
 * drift reconciliation reports, and brick lifecycle operations.
 */

import { createStore as create } from "./create-store.js";
import type { FetchClient } from "@nexus-ai-fs/api-client";
import { createApiAction, categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";
import { useUiStore } from "./ui-store.js";

// =============================================================================
// Types (snake_case matching API wire format)
// =============================================================================

export interface BrickStatusResponse {
  readonly name: string;
  readonly state: string;
  readonly protocol_name: string;
  readonly error: string | null;
  readonly started_at: number | null;
  readonly stopped_at: number | null;
  readonly unmounted_at: number | null;
}

export interface BrickTransitionItem {
  readonly timestamp: number;
  readonly event: string;
  readonly from_state: string;
  readonly to_state: string;
}

export interface BrickDetailResponse extends BrickStatusResponse {
  readonly enabled: boolean;
  readonly depends_on: readonly string[];
  readonly depended_by: readonly string[];
  readonly retry_count: number;
  readonly transitions: readonly BrickTransitionItem[];
}

export interface DriftReportItem {
  readonly brick_name: string;
  readonly spec_state: string;
  readonly actual_state: string;
  readonly action: string;
  readonly detail: string;
}

export interface BricksHealthResponse {
  readonly total: number;
  readonly active: number;
  readonly failed: number;
  readonly bricks: readonly BrickStatusResponse[];
}

export interface DriftReportResponse {
  readonly total_bricks: number;
  readonly drifted: number;
  readonly actions_taken: number;
  readonly errors: number;
  readonly drifts: readonly DriftReportItem[];
  readonly last_reconcile_at: number | null;
  readonly reconcile_count: number;
}

export interface ZoneResponse {
  readonly zone_id: string;
  readonly name: string;
  readonly domain: string | null;
  readonly description: string | null;
  readonly phase: string;
  readonly finalizers: readonly string[];
  readonly is_active: boolean;
  readonly created_at: string;
  readonly updated_at: string;
  readonly limits: Record<string, unknown> | null;
}

interface ZonesListResponse {
  readonly zones: readonly ZoneResponse[];
  readonly total: number;
}

// =============================================================================
// Tab type
// =============================================================================

export type ZoneTab = "zones" | "bricks" | "drift" | "reindex" | "workspaces" | "mcp" | "cache";

// =============================================================================
// Store
// =============================================================================

export interface ZonesState {
  // Zone list
  readonly zones: readonly ZoneResponse[];
  readonly zonesLoading: boolean;

  // Brick list (from health endpoint)
  readonly bricksHealth: BricksHealthResponse | null;
  readonly bricks: readonly BrickStatusResponse[];
  readonly selectedIndex: number;
  readonly isLoading: boolean;
  readonly error: string | null;

  // Active tab
  readonly activeTab: ZoneTab;

  // Brick detail (extended with spec/dependency info)
  readonly brickDetail: BrickDetailResponse | null;
  readonly detailLoading: boolean;

  // Drift report (global, not per-brick)
  readonly driftReport: DriftReportResponse | null;
  readonly driftLoading: boolean;

  // Cache management
  readonly cacheStats: unknown | null;
  readonly cacheStatsLoading: boolean;
  readonly hotFiles: readonly unknown[];
  readonly hotFilesLoading: boolean;

  // Actions
  readonly fetchZones: (client: FetchClient) => Promise<void>;
  readonly fetchBricks: (client: FetchClient) => Promise<void>;
  readonly fetchBrickDetail: (name: string, client: FetchClient) => Promise<void>;
  readonly fetchDrift: (client: FetchClient) => Promise<void>;
  readonly mountBrick: (name: string, client: FetchClient) => Promise<void>;
  readonly unmountBrick: (name: string, client: FetchClient) => Promise<void>;
  readonly unregisterBrick: (name: string, client: FetchClient) => Promise<void>;
  readonly remountBrick: (name: string, client: FetchClient) => Promise<void>;
  readonly resetBrick: (name: string, client: FetchClient) => Promise<void>;
  readonly fetchCacheStats: (client: FetchClient) => Promise<void>;
  readonly fetchHotFiles: (client: FetchClient) => Promise<void>;
  readonly warmupCache: (paths: readonly string[], client: FetchClient) => Promise<void>;
  readonly setSelectedIndex: (index: number) => void;
  readonly setActiveTab: (tab: ZoneTab) => void;
}

const SOURCE = "zones";

export const useZonesStore = create<ZonesState>((set, get) => ({
  zones: [],
  zonesLoading: false,
  bricksHealth: null,
  bricks: [],
  selectedIndex: 0,
  isLoading: false,
  error: null,
  activeTab: "zones",
  brickDetail: null,
  detailLoading: false,
  driftReport: null,
  driftLoading: false,
  cacheStats: null,
  cacheStatsLoading: false,
  hotFiles: [],
  hotFilesLoading: false,

  // =========================================================================
  // Actions migrated to createApiAction
  // =========================================================================

  fetchZones: async (client) => {
    set({ zonesLoading: true, error: null });
    try {
      // Use rawRequest to avoid the FetchClient's automatic 3× retry on 503.
      // The /api/zones endpoint returns 503 when DatabaseLocalAuth is not
      // configured, which is expected for API-key-only server setups.
      const raw = await client.rawRequest("GET", "/api/zones");
      if (raw.status === 503) {
        // Auth provider not available — treat as "no zones"
        set({ zones: [], zonesLoading: false });
        return;
      }
      if (!raw.ok) {
        const body = await raw.json().catch(() => ({ detail: `HTTP ${raw.status}` })) as { detail?: string };
        throw new Error(body.detail ?? `HTTP ${raw.status}`);
      }
      const data = (await raw.json()) as { zones?: ZoneResponse[] };
      set({ zones: data.zones ?? [], zonesLoading: false });
      useUiStore.getState().markDataUpdated("zones");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch zones";
      set({ zones: [], zonesLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchBricks: createApiAction<ZonesState, [FetchClient]>(set, {
    loadingKey: "isLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch bricks",
    action: async (client) => {
      let response: BricksHealthResponse;
      try {
        response = await client.get<BricksHealthResponse>("/api/v2/bricks/health");
      } catch {
        // Health endpoint may 404 — use empty response as fallback
        response = { bricks: [], total: 0, active: 0, failed: 0 };
      }
      let bricks = response.bricks ?? [];
      // Fallback: if health endpoint returns no/empty bricks, synthesize from features
      if (bricks.length === 0) {
        try {
          const features = await client.get<{ enabled_bricks?: readonly string[] }>(
            "/api/v2/features",
          );
          if (features.enabled_bricks && features.enabled_bricks.length > 0) {
            bricks = features.enabled_bricks.map((name) => ({
              name,
              state: "active",
              protocol_name: "brick",
              error: null,
              started_at: null,
              stopped_at: null,
              unmounted_at: null,
            }));
          }
        } catch { /* features endpoint unavailable */ }
      }
      return {
        bricksHealth: { ...response, bricks, total: bricks.length, active: bricks.length },
        bricks,
      };
    },
  }),

  fetchCacheStats: createApiAction<ZonesState, [FetchClient]>(set, {
    loadingKey: "cacheStatsLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch cache stats",
    action: async (client) => {
      const stats = await client.get<unknown>("/api/v2/cache/stats");
      return { cacheStats: stats };
    },
  }),

  fetchHotFiles: createApiAction<ZonesState, [FetchClient]>(set, {
    loadingKey: "hotFilesLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch hot files",
    action: async (client) => {
      const response = await client.get<{ files: readonly unknown[] }>("/api/v2/cache/hot-files");
      return { hotFiles: response.files ?? [] };
    },
  }),

  // =========================================================================
  // Actions with special error-path state — inline with error store integration
  // =========================================================================

  fetchBrickDetail: async (name, client) => {
    set({ detailLoading: true, error: null });

    try {
      const detail = await client.get<BrickDetailResponse>(
        `/api/v2/bricks/${encodeURIComponent(name)}`,
      );
      set({ brickDetail: detail, detailLoading: false });
      useUiStore.getState().markDataUpdated("zones");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch brick detail";
      set({
        brickDetail: null,
        detailLoading: false,
        error: message,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchDrift: async (client) => {
    set({ driftLoading: true, error: null });

    try {
      const report = await client.get<DriftReportResponse>(
        "/api/v2/bricks/drift",
      );
      set({ driftReport: report, driftLoading: false });
      useUiStore.getState().markDataUpdated("zones");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch drift report";
      set({
        driftReport: null,
        driftLoading: false,
        error: message,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  // =========================================================================
  // Actions without loading keys — inline with error store integration
  // =========================================================================

  mountBrick: async (name, client) => {
    set({ error: null });

    try {
      await client.post<void>(
        `/api/v2/bricks/${encodeURIComponent(name)}/mount`,
        {},
      );
      await get().fetchBricks(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to mount brick";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  unmountBrick: async (name, client) => {
    set({ error: null });

    try {
      await client.post<void>(
        `/api/v2/bricks/${encodeURIComponent(name)}/unmount`,
        {},
      );
      await get().fetchBricks(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to unmount brick";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  unregisterBrick: async (name, client) => {
    set({ error: null });

    try {
      await client.post<void>(
        `/api/v2/bricks/${encodeURIComponent(name)}/unregister`,
        {},
      );
      await get().fetchBricks(client);
      // Clamp selectedIndex and clear stale detail after brick removal
      const { bricks, selectedIndex } = get();
      const clamped = Math.min(selectedIndex, Math.max(0, bricks.length - 1));
      set({ selectedIndex: clamped, brickDetail: null });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to unregister brick";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  remountBrick: async (name, client) => {
    set({ error: null });

    try {
      await client.post<void>(
        `/api/v2/bricks/${encodeURIComponent(name)}/remount`,
        {},
      );
      await get().fetchBricks(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to remount brick";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  resetBrick: async (name, client) => {
    set({ error: null });

    try {
      await client.post<void>(
        `/api/v2/bricks/${encodeURIComponent(name)}/reset`,
        {},
      );
      await get().fetchBricks(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to reset brick";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  warmupCache: async (paths, client) => {
    set({ error: null });
    try {
      await client.post("/api/v2/cache/warmup", { paths });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to warmup cache";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  setSelectedIndex: (index) => {
    set({
      selectedIndex: index,
      brickDetail: null,
    });
  },

  setActiveTab: (tab) => {
    set({ activeTab: tab });
  },
}));
