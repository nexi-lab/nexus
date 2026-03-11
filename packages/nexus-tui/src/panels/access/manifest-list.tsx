/**
 * Access manifest list: shows ReBAC tuples as rows.
 */

import React from "react";
import type { AccessManifest } from "../../stores/access-store.js";

interface ManifestListProps {
  readonly manifests: readonly AccessManifest[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function shortId(id: string): string {
  if (id.length <= 16) return id;
  return `${id.slice(0, 12)}..`;
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return "never";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function ManifestList({
  manifests,
  selectedIndex,
  loading,
}: ManifestListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading manifests...</text>
      </box>
    );
  }

  if (manifests.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No access manifests found</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  SUBJECT          RELATION       OBJECT           ZONE       GRANTED BY       EXPIRES"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ---------------  -------------  ---------------  ---------  ---------------  -------"}</text>
      </box>

      {/* Rows */}
      {manifests.map((m, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const zone = m.zone_id ?? "global";

        return (
          <box key={m.manifest_id} height={1} width="100%">
            <text>
              {`${prefix}${shortId(m.subject).padEnd(15)}  ${m.relation.padEnd(13)}  ${shortId(m.object).padEnd(15)}  ${zone.padEnd(9)}  ${shortId(m.granted_by).padEnd(15)}  ${formatTimestamp(m.expires_at)}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
