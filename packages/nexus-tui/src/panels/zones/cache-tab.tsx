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
      <text>{loading() ? "Loading cache stats..." : !stats() ? "No cache data available." : "--- Cache Statistics ---"}</text>
      <text>{stats() ? `  Tracked paths:   ${stats()!.tracked_paths ?? stats()!.total_entries ?? 0}` : ""}</text>
      <text>{stats() ? `  Total accesses:  ${stats()!.total_accesses ?? 0}` : ""}</text>
      <text>{stats() ? `  Total entries:   ${stats()!.total_entries ?? 0}` : ""}</text>
      <text>{stats() ? `  Total size:      ${stats()!.total_size_bytes ?? 0} bytes` : ""}</text>
      <text>{stats() ? `  Hit rate:        ${((stats()!.hit_rate as number ?? 0) * 100).toFixed(1)}%` : ""}</text>
      <text>{stats() ? `  Window:          ${stats()!.window_seconds ?? 300}s` : ""}</text>
      <text>{stats() ? `  Hot threshold:   ${stats()!.hot_threshold ?? 10} accesses` : ""}</text>
      <text>{""}</text>
      <text>{hotFiles().length > 0 ? "--- Hot Files ---" : "No hot files tracked yet."}</text>
      {hotFiles().slice(0, 10).map((f, i) => (
        <text key={`hf-${i}`}>{`  ${f.path ?? "?"} (${f.access_count ?? 0} hits)`}</text>
      ))}
    </box>
  );
}
