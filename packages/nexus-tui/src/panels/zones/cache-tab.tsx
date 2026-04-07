import type { JSX } from "solid-js";

import { useZonesStore } from "../../stores/zones-store.js";

/**
 * Cache statistics tab — reads directly from store for reactive updates.
 */
export function CacheTab(): JSX.Element {
  const stats = () => useZonesStore((s) => s.cacheStats) as Record<string, unknown> | null;
  const loading = () => useZonesStore((s) => s.cacheStatsLoading);
  const hotFiles = () => useZonesStore((s) => s.hotFiles) as readonly { path?: string; access_count?: number }[];

  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>{loading() ? "Loading cache stats..." : !stats() ? "No cache data available." : "--- File Access Tracker ---"}</text>
      <text>{stats() ? `  Tracked hot paths:  ${stats()!.tracked_paths ?? stats()!.total_entries ?? 0}` : ""}</text>
      <text>{stats() ? `  Total accesses:     ${stats()!.total_accesses ?? 0}` : ""}</text>
      <text>{stats() ? `  Window:             ${stats()!.window_seconds ?? 300}s` : ""}</text>
      <text>{stats() ? `  Hot threshold:      ${stats()!.hot_threshold ?? 10} accesses` : ""}</text>
      <text>{""}</text>
      <text>{"--- Dragonfly Cache (backend) ---"}</text>
      <text>{"  Use 'docker exec <dragonfly> redis-cli INFO' for full stats."}</text>
      <text>{"  Cache is active — stats not exposed via HTTP API yet."}</text>
      <text>{""}</text>
      <text>{hotFiles().length > 0 ? "--- Hot Files ---" : "No hot files tracked yet."}</text>
      {hotFiles().slice(0, 10).map((f, i) => (
        <text key={`hf-${i}`}>{`  ${f.path ?? "?"} (${f.access_count ?? 0} hits)`}</text>
      ))}
    </box>
  );
}
