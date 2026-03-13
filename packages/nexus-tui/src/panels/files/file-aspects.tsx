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
  if (!item.path || !item.zoneId) return null;
  const pathHash = crypto
    .createHash("sha256")
    .update(item.path)
    .digest("hex")
    .slice(0, 32);
  return `urn:nexus:file:${item.zoneId}:${pathHash}`;
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

  if (!item) {
    return <text>No file selected</text>;
  }

  if (!urn) {
    return <text>{"Cannot compute URN (missing zone)"}</text>;
  }

  if (loading) {
    return <text>Loading aspects...</text>;
  }

  const aspectNames = aspectsCache.get(urn) ?? [];

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
