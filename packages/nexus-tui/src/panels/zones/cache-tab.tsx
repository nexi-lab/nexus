import React from "react";
import { formatSize } from "../../shared/utils/format-size.js";
import { statusColor } from "../../shared/theme.js";

interface CacheStats {
  readonly total_entries?: number;
  readonly total_size_bytes?: number;
  readonly hit_rate?: number;
  readonly miss_rate?: number;
  readonly eviction_count?: number;
  readonly layers?: ReadonlyArray<{
    readonly name: string;
    readonly entries: number;
    readonly size_bytes: number;
    readonly hit_rate: number;
  }>;
}

interface HotFile {
  readonly path?: string;
  readonly access_count?: number;
}

interface CacheTabProps {
  readonly stats: unknown | null;
  readonly hotFiles: readonly unknown[];
  readonly loading: boolean;
}

function hitRateColor(rate: number): string | undefined {
  if (rate > 0.8) return statusColor.healthy;
  if (rate > 0.5) return statusColor.warning;
  return statusColor.error;
}

export function CacheTab({ stats, hotFiles, loading }: CacheTabProps): React.ReactNode {
  if (loading) return <text>Loading cache stats...</text>;
  if (!stats) return <text>No cache data available.</text>;

  const s = stats as CacheStats;
  const files = hotFiles as readonly HotFile[];

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%"><text>--- Cache Statistics ---</text></box>

      {/* Summary stats */}
      {s.total_entries != null && (
        <box height={1} width="100%">
          <text>{`  Total entries:   ${s.total_entries.toLocaleString()}`}</text>
        </box>
      )}
      {s.total_size_bytes != null && (
        <box height={1} width="100%">
          <text>{`  Total size:      ${formatSize(s.total_size_bytes)}`}</text>
        </box>
      )}
      {s.hit_rate != null && (
        <box height={1} width="100%">
          <text>
            {"  Hit rate:        "}
            <span foregroundColor={hitRateColor(s.hit_rate)}>{`${(s.hit_rate * 100).toFixed(1)}%`}</span>
          </text>
        </box>
      )}
      {s.miss_rate != null && (
        <box height={1} width="100%">
          <text>{`  Miss rate:       ${(s.miss_rate * 100).toFixed(1)}%`}</text>
        </box>
      )}
      {s.eviction_count != null && (
        <box height={1} width="100%">
          <text>{`  Evictions:       ${s.eviction_count.toLocaleString()}`}</text>
        </box>
      )}

      {/* Layer table */}
      {s.layers && s.layers.length > 0 && (
        <>
          <box height={1} width="100%"><text>{""}</text></box>
          <box height={1} width="100%"><text>--- Cache Layers ---</text></box>
          <box height={1} width="100%">
            <text>{"  NAME                 ENTRIES     SIZE         HIT RATE"}</text>
          </box>
          <box height={1} width="100%">
            <text>{"  -------------------  ----------  -----------  --------"}</text>
          </box>
          {s.layers.map((layer) => (
            <box key={layer.name} height={1} width="100%">
              <text>
                {`  ${layer.name.padEnd(19)}  ${String(layer.entries).padEnd(10)}  ${formatSize(layer.size_bytes).padEnd(11)}  `}
                <span foregroundColor={hitRateColor(layer.hit_rate)}>{`${(layer.hit_rate * 100).toFixed(1)}%`}</span>
              </text>
            </box>
          ))}
        </>
      )}

      {/* Hot files */}
      {files.length > 0 && (
        <>
          <box height={1} width="100%"><text>{""}</text></box>
          <box height={1} width="100%"><text>--- Hot Files ---</text></box>
          {files.slice(0, 10).map((file, i) => {
            const path = file.path ?? "unknown";
            const count = file.access_count ?? 0;
            return (
              <box key={`hf-${i}`} height={1} width="100%">
                <text>{`  ${path} (${count} hits)`}</text>
              </box>
            );
          })}
        </>
      )}
    </box>
  );
}
