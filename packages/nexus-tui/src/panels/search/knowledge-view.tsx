/**
 * Knowledge graph view: entity detail (as dict) and neighbors list with depth info.
 */

import React from "react";
import type { KnowledgeEntity, NeighborEntry } from "../../stores/search-store.js";

interface KnowledgeViewProps {
  readonly entity: KnowledgeEntity | null;
  readonly neighbors: readonly NeighborEntry[];
  readonly knowledgeSearchResult: KnowledgeEntity | null;
  readonly loading: boolean;
}

function formatEntityDict(entity: KnowledgeEntity): string {
  const entries = Object.entries(entity);
  if (entries.length === 0) return "{}";
  return entries
    .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
    .join(", ");
}

function truncateValue(value: unknown, maxLen: number): string {
  const str = JSON.stringify(value);
  if (str.length <= maxLen) return str;
  return `${str.slice(0, maxLen - 3)}...`;
}

export function KnowledgeView({
  entity,
  neighbors,
  knowledgeSearchResult,
  loading,
}: KnowledgeViewProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading knowledge graph...</text>
      </box>
    );
  }

  if (!entity && !knowledgeSearchResult) {
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
          {Object.entries(entity).map(([key, value]) => (
            <box key={key} height={1} width="100%">
              <text>{`  ${key}: ${truncateValue(value, 60)}`}</text>
            </box>
          ))}
        </>
      )}

      {/* Neighbors */}
      {neighbors.length > 0 && (
        <>
          <box height={1} width="100%" marginTop={1}>
            <text>{`--- Neighbors (${neighbors.length}) ---`}</text>
          </box>
          {neighbors.map((n, i) => {
            const entitySummary = formatEntityDict(n.entity);
            const pathStr = n.path.join(" -> ");
            return (
              <box key={i} height={1} width="100%">
                <text>{`  [depth=${n.depth}] ${truncateValue(entitySummary, 50)}  path: ${pathStr}`}</text>
              </box>
            );
          })}
        </>
      )}

      {/* Knowledge search result (single entity) */}
      {!entity && knowledgeSearchResult && (
        <>
          <box height={1} width="100%">
            <text>--- Graph Search Result ---</text>
          </box>
          {Object.entries(knowledgeSearchResult).map(([key, value]) => (
            <box key={key} height={1} width="100%">
              <text>{`  ${key}: ${truncateValue(value, 60)}`}</text>
            </box>
          ))}
        </>
      )}
    </scrollbox>
  );
}
