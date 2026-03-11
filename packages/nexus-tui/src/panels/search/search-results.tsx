/**
 * Search results list: type badge, title, snippet (truncated), score.
 */

import React from "react";
import type { SearchResult } from "../../stores/search-store.js";

interface SearchResultsProps {
  readonly results: readonly SearchResult[];
  readonly total: number;
  readonly selectedIndex: number;
  readonly loading: boolean;
}

const TYPE_BADGES: Readonly<Record<string, string>> = {
  file: "[FIL]",
  entity: "[ENT]",
  memory: "[MEM]",
  playbook: "[PLY]",
  agent: "[AGT]",
};

function truncateSnippet(snippet: string, maxLen: number): string {
  if (snippet.length <= maxLen) return snippet;
  return `${snippet.slice(0, maxLen - 3)}...`;
}

function formatScore(score: number): string {
  return score.toFixed(2);
}

export function SearchResults({
  results,
  total,
  selectedIndex,
  loading,
}: SearchResultsProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Searching...</text>
      </box>
    );
  }

  if (results.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No results found</text>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={1} width="100%">
        <text>{`Results: ${total} found`}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  TYPE   SCORE  TITLE                          SNIPPET"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  -----  -----  -----------------------------  --------------------------------"}</text>
      </box>

      {/* Result rows */}
      <scrollbox flexGrow={1} width="100%">
        {results.map((result, i) => {
          const isSelected = i === selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const badge = TYPE_BADGES[result.type] ?? `[${result.type.slice(0, 3).toUpperCase()}]`;
          const title = result.title.length > 29
            ? `${result.title.slice(0, 26)}...`
            : result.title;
          const snippet = truncateSnippet(result.snippet, 32);

          return (
            <box key={result.id} height={1} width="100%">
              <text>
                {`${prefix}${badge}  ${formatScore(result.score).padEnd(5)}  ${title.padEnd(29)}  ${snippet}`}
              </text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
