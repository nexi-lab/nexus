import type { JSX } from "solid-js";
/**
 * Knowledge graph view: entity detail (as dict) and neighbors list with depth info.
 */

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

export function KnowledgeView(props: KnowledgeViewProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading knowledge graph..."
          : !props.entity && !props.knowledgeSearchResult
            ? "Search or select a result to explore the knowledge graph"
            : "Knowledge Graph"}
      </text>

      {(() => {
        if (props.loading || (!props.entity && !props.knowledgeSearchResult)) return null;

        return (
          <scrollbox flexGrow={1} width="100%">
            {/* Entity detail */}
            {props.entity && (
              <>
                <box height={1} width="100%">
                  <text>--- Entity Detail ---</text>
                </box>
                {Object.entries(props.entity).map(([key, value]) => (
                  <box height={1} width="100%">
                    <text>{`  ${key}: ${truncateValue(value, 60)}`}</text>
                  </box>
                ))}
              </>
            )}

            {/* Neighbors */}
            {props.neighbors.length > 0 && (
              <>
                <box height={1} width="100%" marginTop={1}>
                  <text>{`--- Neighbors (${props.neighbors.length}) ---`}</text>
                </box>
                {props.neighbors.map((n, i) => {
                  const entitySummary = formatEntityDict(n.entity);
                  const pathStr = n.path.join(" -> ");
                  return (
                    <box height={1} width="100%">
                      <text>{`  [depth=${n.depth}] ${truncateValue(entitySummary, 50)}  path: ${pathStr}`}</text>
                    </box>
                  );
                })}
              </>
            )}

            {/* Knowledge search result (single entity) */}
            {!props.entity && props.knowledgeSearchResult && (
              <>
                <box height={1} width="100%">
                  <text>--- Graph Search Result ---</text>
                </box>
                {Object.entries(props.knowledgeSearchResult).map(([key, value]) => (
                  <box height={1} width="100%">
                    <text>{`  ${key}: ${truncateValue(value, 60)}`}</text>
                  </box>
                ))}
              </>
            )}
          </scrollbox>
        );
      })()}
    </box>
  );
}
