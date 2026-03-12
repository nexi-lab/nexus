/**
 * Access manifest list: shows manifests with name, agent, zone, status, entries count, validity.
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

function formatTimestamp(ts: string): string {
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
        <text>{"  NAME             AGENT            ZONE             STATUS     ENTRIES  VALID FROM         VALID UNTIL"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ---------------  ---------------  ---------------  ---------  -------  -----------------  -----------------"}</text>
      </box>

      {/* Rows */}
      {manifests.map((m, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";
        const entriesCount = String(m.entries?.length ?? "-");

        return (
          <box key={m.manifest_id} height={1} width="100%">
            <text>
              {`${prefix}${shortId(m.name).padEnd(15)}  ${shortId(m.agent_id).padEnd(15)}  ${shortId(m.zone_id).padEnd(15)}  ${m.status.padEnd(9)}  ${entriesCount.padEnd(7)}  ${formatTimestamp(m.valid_from).padEnd(17)}  ${formatTimestamp(m.valid_until)}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
