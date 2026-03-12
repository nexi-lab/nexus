/**
 * Zustand store for Zones & Federation panel.
 *
 * Manages zone listing, brick health, individual brick detail,
 * drift reconciliation reports, and brick lifecycle operations.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

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

export type ZoneTab = "zones" | "bricks" | "drift";

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

  // Brick detail
  readonly brickDetail: BrickStatusResponse | null;
  readonly detailLoading: boolean;

  // Drift report (global, not per-brick)
  readonly driftReport: DriftReportResponse | null;
  readonly driftLoading: boolean;

  // Actions
  readonly fetchZones: (client: FetchClient) => Promise<void>;
  readonly fetchBricks: (client: FetchClient) => Promise<void>;
  readonly fetchBrickDetail: (name: string, client: FetchClient) => Promise<void>;
  readonly fetchDrift: (client: FetchClient) => Promise<void>;
  readonly remountBrick: (name: string, client: FetchClient) => Promise<void>;
  readonly resetBrick: (name: string, client: FetchClient) => Promise<void>;
  readonly setSelectedIndex: (index: number) => void;
  readonly setActiveTab: (tab: ZoneTab) => void;
}

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

  fetchZones: async (client) => {
    set({ zonesLoading: true, error: null });

    try {
      const response = await client.get<ZonesListResponse>("/api/zones");
      set({ zones: response.zones ?? [], zonesLoading: false });
    } catch (err) {
      set({
        zonesLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch zones",
      });
    }
  },

  fetchBricks: async (client) => {
    set({ isLoading: true, error: null });

    try {
      const response = await client.get<BricksHealthResponse>(
        "/api/v2/bricks/health",
      );
      const bricks = response.bricks ?? [];
      set({ bricksHealth: response, bricks, isLoading: false });
    } catch (err) {
      set({
        isLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch bricks",
      });
    }
  },

  fetchBrickDetail: async (name, client) => {
    set({ detailLoading: true, error: null });

    try {
      const detail = await client.get<BrickStatusResponse>(
        `/api/v2/bricks/${encodeURIComponent(name)}`,
      );
      set({ brickDetail: detail, detailLoading: false });
    } catch (err) {
      set({
        brickDetail: null,
        detailLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch brick detail",
      });
    }
  },

  fetchDrift: async (client) => {
    set({ driftLoading: true, error: null });

    try {
      const report = await client.get<DriftReportResponse>(
        "/api/v2/bricks/drift",
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

  remountBrick: async (name, client) => {
    set({ error: null });

    try {
      await client.post<void>(
        `/api/v2/bricks/${encodeURIComponent(name)}/remount`,
        {},
      );
      await get().fetchBricks(client);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to remount brick",
      });
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
      set({
        error: err instanceof Error ? err.message : "Failed to reset brick",
      });
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
