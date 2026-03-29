/**
 * Access manifest list: shows manifests with name, agent, zone, status, entries count, validity.
 * When entries are loaded (via fetchManifestDetail), shows the tuple entries inline
 * for the selected manifest — this serves as the tuple browser.
 */

import React from "react";
import type { AccessManifest } from "../../stores/access-store.js";
import { EmptyState } from "../../shared/components/empty-state.js";

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
    return <EmptyState message="No manifests found." hint="Press c to create a manifest." />;
  }

  const selected = manifests[selectedIndex];
  const entries = selected?.entries;

  return (
    <box height="100%" width="100%" flexDirection="column">
      <scrollbox flexGrow={1} width="100%">
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

      {/* Tuple entries for selected manifest (shown when loaded via Enter) */}
      {entries && entries.length > 0 && (
        <box height={Math.min(entries.length + 2, 8)} width="100%" flexDirection="column" borderStyle="single">
          <box height={1} width="100%">
            <text>{`Entries (tuples) for ${selected.name}:`}</text>
          </box>
          {entries.map((e, i) => {
            const rateStr = e.max_calls_per_minute
              ? `  rate=${e.max_calls_per_minute}/min`
              : "";
            return (
              <box key={`${e.tool_pattern}-${i}`} height={1} width="100%">
                <text>
                  {`  ${e.tool_pattern.padEnd(30)} ${e.permission.padEnd(6)}${rateStr}`}
                </text>
              </box>
            );
          })}
        </box>
      )}

      {entries !== undefined && entries.length === 0 && (
        <box height={1} width="100%">
          <text>{`No entries (tuples) for ${selected?.name ?? "manifest"}`}</text>
        </box>
      )}
    </box>
  );
}
