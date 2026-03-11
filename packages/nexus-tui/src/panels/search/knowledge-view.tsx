/**
 * Knowledge graph view: entity detail and neighbors list.
 */

import React from "react";
import type { KnowledgeEntity } from "../../stores/search-store.js";

interface KnowledgeViewProps {
  readonly entity: KnowledgeEntity | null;
  readonly neighbors: readonly KnowledgeEntity[];
  readonly entities: readonly KnowledgeEntity[];
  readonly loading: boolean;
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function formatProperties(props: Record<string, unknown>): string {
  const entries = Object.entries(props);
  if (entries.length === 0) return "(none)";
  return entries
    .map(([key, value]) => `${key}=${JSON.stringify(value)}`)
    .join(", ");
}

export function KnowledgeView({
  entity,
  neighbors,
  entities,
  loading,
}: KnowledgeViewProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading knowledge graph...</text>
      </box>
    );
  }

  if (!entity && entities.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Search or select a result to explore the knowledge graph</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Entity detail */}
      {entity && (
        <>
          <box height={1} width="100%">
            <text>--- Entity Detail ---</text>
          </box>
          <box height={1} width="100%">
            <text>{`ID:   ${entity.entity_id}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`Name: ${entity.name}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`Type: ${entity.type}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`Properties: ${formatProperties(entity.properties)}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`Created: ${formatTimestamp(entity.created_at)}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`Updated: ${formatTimestamp(entity.updated_at)}`}</text>
          </box>
        </>
      )}

      {/* Neighbors */}
      {neighbors.length > 0 && (
        <>
          <box height={1} width="100%" marginTop={1}>
            <text>{`--- Neighbors (${neighbors.length}) ---`}</text>
          </box>
          {neighbors.map((n) => (
            <box key={n.entity_id} height={1} width="100%">
              <text>{`  [${n.type}] ${n.name} (${n.entity_id})`}</text>
            </box>
          ))}
        </>
      )}

      {/* Knowledge search results */}
      {!entity && entities.length > 0 && (
        <>
          <box height={1} width="100%">
            <text>{`--- Knowledge Entities (${entities.length}) ---`}</text>
          </box>
          {entities.map((e) => (
            <box key={e.entity_id} height={1} width="100%">
              <text>{`  [${e.type}] ${e.name} - ${formatTimestamp(e.updated_at)}`}</text>
            </box>
          ))}
        </>
      )}
    </scrollbox>
  );
}
