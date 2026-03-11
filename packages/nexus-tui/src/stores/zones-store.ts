/**
 * Zustand store for Zones & Federation panel.
 *
 * Manages brick/zone lifecycle (list, health, mounts, drift, sync)
 * and per-brick detail views.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

// =============================================================================
// Types (snake_case matching API wire format)
// =============================================================================

export interface Brick {
  readonly brick_id: string;
  readonly zone_id: string;
  readonly brick_type: string;
  readonly status: "online" | "offline" | "degraded" | "syncing";
  readonly address: string;
  readonly capacity_bytes: number;
  readonly used_bytes: number;
  readonly last_seen: string | null;
}

export interface HealthCheck {
  readonly name: string;
  readonly status: "pass" | "fail" | "warn";
  readonly message: string;
}

export interface BrickHealth {
  readonly brick_id: string;
  readonly status: "healthy" | "degraded" | "unhealthy" | "unknown";
  readonly latency_ms: number;
  readonly error_rate: number;
  readonly last_check: string;
  readonly checks: readonly HealthCheck[];
}

export interface MountPoint {
  readonly path: string;
  readonly brick_id: string;
  readonly zone_id: string;
  readonly mount_type: "read" | "write" | "readwrite";
  readonly mounted_at: string;
}

export interface DriftReport {
  readonly brick_id: string;
  readonly has_drift: boolean;
  readonly drift_count: number;
  readonly last_checked: string;
  readonly drifted_paths: readonly string[];
}

interface BrickListResponse {
  readonly bricks: readonly Brick[];
  readonly total: number;
}

interface MountListResponse {
  readonly mounts: readonly MountPoint[];
}

// =============================================================================
// Tab type
// =============================================================================

export type ZoneTab = "overview" | "health" | "mounts" | "drift";

// =============================================================================
// Store
// =============================================================================

export interface ZonesState {
  // Brick list
  readonly bricks: readonly Brick[];
  readonly selectedBrick: Brick | null;
  readonly selectedIndex: number;
  readonly isLoading: boolean;
  readonly error: string | null;

  // Active detail tab
  readonly activeTab: ZoneTab;

  // Detail views
  readonly brickHealth: BrickHealth | null;
  readonly healthLoading: boolean;
  readonly mountPoints: readonly MountPoint[];
  readonly mountsLoading: boolean;
  readonly driftReport: DriftReport | null;
  readonly driftLoading: boolean;

  // Actions
  readonly fetchBricks: (client: FetchClient) => Promise<void>;
  readonly fetchBrickHealth: (brickId: string, client: FetchClient) => Promise<void>;
  readonly fetchMounts: (brickId: string, client: FetchClient) => Promise<void>;
  readonly fetchDrift: (brickId: string, client: FetchClient) => Promise<void>;
  readonly triggerSync: (brickId: string, client: FetchClient) => Promise<void>;
  readonly setSelectedIndex: (index: number) => void;
  readonly setActiveTab: (tab: ZoneTab) => void;
}

export const useZonesStore = create<ZonesState>((set, get) => ({
  bricks: [],
  selectedBrick: null,
  selectedIndex: 0,
  isLoading: false,
  error: null,
  activeTab: "overview",
  brickHealth: null,
  healthLoading: false,
  mountPoints: [],
  mountsLoading: false,
  driftReport: null,
  driftLoading: false,

  fetchBricks: async (client) => {
    set({ isLoading: true, error: null });

    try {
      const response = await client.get<BrickListResponse>("/api/v2/bricks");
      const bricks = response.bricks ?? [];
      set({ bricks, isLoading: false });
    } catch (err) {
      set({
        isLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch bricks",
      });
    }
  },

  fetchBrickHealth: async (brickId, client) => {
    set({ healthLoading: true, error: null });

    try {
      const health = await client.get<BrickHealth>(
        `/api/v2/bricks/${encodeURIComponent(brickId)}/health`,
      );
      set({ brickHealth: health, healthLoading: false });
    } catch (err) {
      set({
        brickHealth: null,
        healthLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch brick health",
      });
    }
  },

  fetchMounts: async (brickId, client) => {
    set({ mountsLoading: true, error: null });

    try {
      const response = await client.get<MountListResponse>(
        `/api/v2/bricks/${encodeURIComponent(brickId)}/mount`,
      );
      set({ mountPoints: response.mounts ?? [], mountsLoading: false });
    } catch (err) {
      set({
        mountPoints: [],
        mountsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch mounts",
      });
    }
  },

  fetchDrift: async (brickId, client) => {
    set({ driftLoading: true, error: null });

    try {
      const report = await client.get<DriftReport>(
        `/api/v2/bricks/${encodeURIComponent(brickId)}/drift`,
      );
      set({ driftReport: report, driftLoading: false });
    } catch (err) {
      set({
        driftReport: null,
        driftLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch drift report",
      });
    }
  },

  triggerSync: async (brickId, client) => {
    set({ error: null });

    try {
      await client.post<void>(
        `/api/v2/bricks/${encodeURIComponent(brickId)}/sync`,
        {},
      );
      await get().fetchBricks(client);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to trigger sync",
      });
    }
  },

  setSelectedIndex: (index) => {
    const { bricks } = get();
    const brick = bricks[index] ?? null;
    set({
      selectedIndex: index,
      selectedBrick: brick,
      brickHealth: null,
      mountPoints: [],
      driftReport: null,
    });
  },

  setActiveTab: (tab) => {
    set({ activeTab: tab });
  },
}));
