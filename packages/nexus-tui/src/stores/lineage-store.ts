/**
 * Zustand store for lineage data: upstream inputs, downstream dependents.
 * Issue #3417.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus-ai-fs/api-client";
import { useErrorStore } from "./error-store.js";

// =============================================================================
// Types
// =============================================================================

export interface UpstreamEntry {
  readonly path: string;
  readonly version: number;
  readonly etag: string;
  readonly access_type: string;
}

export interface LineageData {
  readonly upstream: readonly UpstreamEntry[];
  readonly agent_id: string;
  readonly agent_generation: number | null;
  readonly operation: string;
  readonly duration_ms: number | null;
  readonly truncated: boolean;
}

export interface DownstreamEntry {
  readonly downstream_urn: string;
  readonly downstream_path: string | null;
  readonly upstream_version: number;
  readonly upstream_etag: string;
  readonly agent_id: string;
  readonly created_at: string | null;
}

// =============================================================================
// Store
// =============================================================================

export interface LineageState {
  // Cache keyed by URN
  readonly lineageCache: ReadonlyMap<string, LineageData | null>;
  readonly downstreamCache: ReadonlyMap<string, readonly DownstreamEntry[]>;
  readonly loading: boolean;
  readonly error: string | null;

  // Actions
  readonly fetchLineage: (urn: string, path: string, client: FetchClient) => Promise<void>;
  readonly clearCache: () => void;
}

export const useLineageStore = create<LineageState>((set, get) => ({
  lineageCache: new Map(),
  downstreamCache: new Map(),
  loading: false,
  error: null,

  fetchLineage: async (urn: string, path: string, client: FetchClient) => {
    // Skip if already cached
    if (get().lineageCache.has(urn)) return;

    set({ loading: true, error: null });
    try {
      // Fetch upstream lineage
      const encodedUrn = encodeURIComponent(urn);
      let lineageData: LineageData | null = null;
      try {
        const resp = await client.get<LineageData & { entity_urn: string }>(
          `/api/v2/lineage/${encodedUrn}`,
        );
        lineageData = {
          upstream: resp.upstream ?? [],
          agent_id: resp.agent_id ?? "",
          agent_generation: resp.agent_generation ?? null,
          operation: resp.operation ?? "",
          duration_ms: resp.duration_ms ?? null,
          truncated: resp.truncated ?? false,
        };
      } catch {
        // 404 = no lineage, not an error
        lineageData = null;
      }

      // Fetch downstream dependents
      let downstream: DownstreamEntry[] = [];
      try {
        const encodedPath = encodeURIComponent(path);
        const dsResp = await client.get<{ downstream: DownstreamEntry[] }>(
          `/api/v2/lineage/downstream/query?path=${encodedPath}`,
        );
        downstream = dsResp.downstream ?? [];
      } catch {
        // best effort
      }

      const newLineageCache = new Map(get().lineageCache);
      newLineageCache.set(urn, lineageData);
      const newDownstreamCache = new Map(get().downstreamCache);
      newDownstreamCache.set(urn, downstream);

      set({
        lineageCache: newLineageCache,
        downstreamCache: newDownstreamCache,
        loading: false,
      });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      set({ loading: false, error: msg });
      useErrorStore.getState().pushError({
        title: "Lineage fetch failed",
        detail: msg,
        category: "api",
      });
    }
  },

  clearCache: () => {
    set({
      lineageCache: new Map(),
      downstreamCache: new Map(),
    });
  },
}));
