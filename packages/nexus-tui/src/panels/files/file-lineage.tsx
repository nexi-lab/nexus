/**
 * Lineage sub-view for the Files panel.
 * Shows upstream inputs, downstream dependents, and agent provenance.
 * Issue #3417.
 */

import { createEffect } from "solid-js";
import type { JSX } from "solid-js";
import crypto from "node:crypto";
import type { FileItem } from "../../stores/files-store.js";
import { useLineageStore } from "../../stores/lineage-store.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { statusColor } from "../../shared/theme.js";

interface FileLineageProps {
  readonly item: FileItem | null;
}

function computeUrn(item: FileItem): string | null {
  if (!item.path) return null;
  const zone = item.zoneId || "default";
  const pathHash = crypto
    .createHash("sha256")
    .update(item.path)
    .digest("hex")
    .slice(0, 32);
  return `urn:nexus:file:${zone}:${pathHash}`;
}

function truncate(value: string, max: number = 20): string {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

export function FileLineage({ item }: FileLineageProps): JSX.Element {
  const client = useApi();
  const lineageCache = useLineageStore((s) => s.lineageCache);
  const downstreamCache = useLineageStore((s) => s.downstreamCache);
  const loading = useLineageStore((s) => s.loading);
  const fetchLineage = useLineageStore((s) => s.fetchLineage);

  const urn = item ? computeUrn(item) : null;

  createEffect(() => {
    if (client && urn && item?.path) {
      fetchLineage(urn, item.path, client);
    }
  });

  if (!item) {
    return <text>No file selected</text>;
  }

  if (!urn) {
    return <text>Cannot compute URN</text>;
  }

  if (loading && !lineageCache.has(urn)) {
    return <text>Loading lineage...</text>;
  }

  const lineage = lineageCache.get(urn) ?? undefined;
  const downstream = downstreamCache.get(urn) ?? [];
  const hasLineage = lineage !== undefined && lineage !== null;
  const hasDownstream = downstream.length > 0;

  if (!hasLineage && !hasDownstream) {
    return (
      <box flexDirection="column" height="100%" width="100%">
        <text>{"─── Lineage ───"}</text>
        <text> </text>
        <text>{"  No lineage recorded for this file."}</text>
        <text> </text>
        <text foregroundColor={statusColor.dim}>
          {"  Agents declare lineage via scopes or"}
        </text>
        <text foregroundColor={statusColor.dim}>
          {"  PUT /api/v2/lineage/{urn}"}
        </text>
      </box>
    );
  }

  return (
    <box flexDirection="column" height="100%" width="100%">
      <text>{"─── Lineage ───"}</text>

      {/* Producer info */}
      {hasLineage && lineage && (
        <>
          <box height={1} width="100%">
            <text>
              <span foregroundColor={statusColor.dim}>{"Agent    "}</span>
              <span foregroundColor={statusColor.identity}>{lineage.agent_id || "unknown"}</span>
            </text>
          </box>
          <box height={1} width="100%">
            <text>
              <span foregroundColor={statusColor.dim}>{"Op       "}</span>
              <span>{lineage.operation || "write"}</span>
            </text>
          </box>
          {lineage.agent_generation != null && (
            <box height={1} width="100%">
              <text>
                <span foregroundColor={statusColor.dim}>{"Gen      "}</span>
                <span>{`#${lineage.agent_generation}`}</span>
              </text>
            </box>
          )}
          <text> </text>
        </>
      )}

      {/* Upstream inputs */}
      {hasLineage && lineage && lineage.upstream.length > 0 && (
        <>
          <text foregroundColor={statusColor.info}>
            {`▸ Upstream (${lineage.upstream.length})${lineage.truncated ? " [truncated]" : ""}`}
          </text>
          {lineage.upstream.slice(0, 15).map((u, i) => (
            <box key={`up-${i}`} height={1} width="100%">
              <text>
                <span foregroundColor={statusColor.reference}>{"  "}{truncate(u.path, 34)}</span>
                <span foregroundColor={statusColor.dim}>{` v${u.version}`}</span>
              </text>
            </box>
          ))}
          {lineage.upstream.length > 15 && (
            <text foregroundColor={statusColor.dim}>
              {`  ... +${lineage.upstream.length - 15} more`}
            </text>
          )}
          <text> </text>
        </>
      )}

      {/* Downstream dependents */}
      {hasDownstream && (
        <>
          <text foregroundColor={statusColor.info}>
            {`▸ Downstream (${downstream.length})`}
          </text>
          {downstream.slice(0, 10).map((d, i) => (
            <box key={`dn-${i}`} height={1} width="100%">
              <text>
                <span foregroundColor={statusColor.reference}>
                  {"  "}{truncate(d.downstream_path || d.downstream_urn, 30)}
                </span>
                {d.agent_id && (
                  <span foregroundColor={statusColor.dim}>{` by ${d.agent_id}`}</span>
                )}
              </text>
            </box>
          ))}
          {downstream.length > 10 && (
            <text foregroundColor={statusColor.dim}>
              {`  ... +${downstream.length - 10} more`}
            </text>
          )}
        </>
      )}
    </box>
  );
}
