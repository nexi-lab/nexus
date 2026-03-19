/**
 * Aspects sub-view for the Files panel.
 * Shows all aspects attached to the selected file, lazy-loaded from API.
 * Issue #2930.
 */

import React, { useEffect } from "react";
import crypto from "node:crypto";
import type { FileItem } from "../../stores/files-store.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { useApi } from "../../shared/hooks/use-api.js";

interface FileAspectsProps {
  readonly item: FileItem | null;
}

function computeUrn(item: FileItem): string | null {
  if (!item.path) return null;
  // Use "default" zone when zone isolation is not configured
  const zone = item.zoneId || "default";
  const pathHash = crypto
    .createHash("sha256")
    .update(item.path)
    .digest("hex")
    .slice(0, 32);
  return `urn:nexus:file:${zone}:${pathHash}`;
}

export function FileAspects({ item }: FileAspectsProps): React.ReactNode {
  const client = useApi();
  const aspectsCache = useKnowledgeStore((s) => s.aspectsCache);
  const aspectDetailCache = useKnowledgeStore((s) => s.aspectDetailCache);
  const loading = useKnowledgeStore((s) => s.aspectsLoading);
  const fetchAspects = useKnowledgeStore((s) => s.fetchAspects);
  const fetchAspectDetail = useKnowledgeStore((s) => s.fetchAspectDetail);

  const urn = item ? computeUrn(item) : null;

  useEffect(() => {
    if (client && urn) {
      fetchAspects(urn, client);
    }
  }, [client, urn, fetchAspects]);

  // Fetch detail for each aspect once names are loaded
  const aspectNames = urn ? (aspectsCache.get(urn) ?? []) : [];
  useEffect(() => {
    if (client && urn && aspectNames.length > 0) {
      for (const name of aspectNames) {
        fetchAspectDetail(urn, name, client);
      }
    }
  }, [client, urn, aspectNames.length, fetchAspectDetail]);

  if (!item) {
    return <text>No file selected</text>;
  }

  if (!urn) {
    return <text>{"Cannot compute URN (missing zone)"}</text>;
  }

  if (loading) {
    return <text>Loading aspects...</text>;
  }

  if (aspectNames.length === 0) {
    return (
      <box flexDirection="column" height="100%" width="100%">
        <text>{"─── Aspects ───"}</text>
        <text>{"No aspects attached"}</text>
      </box>
    );
  }

  return (
    <box flexDirection="column" height="100%" width="100%">
      <text>{`─── Aspects (${aspectNames.length}) ───`}</text>
      {aspectNames.map((name) => {
        const key = `${urn}::${name}`;
        const detail = aspectDetailCache.get(key);
        return (
          <box key={name} flexDirection="column">
            <text>{`  * ${name}`}</text>
            {detail ? (
              <text>{`    v${detail.version} by ${detail.createdBy}`}</text>
            ) : null}
          </box>
        );
      })}
    </box>
  );
}
