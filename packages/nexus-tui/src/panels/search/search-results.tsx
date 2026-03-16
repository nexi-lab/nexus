/**
 * Search results list: path, chunk_text (truncated), score, line range.
 */

import React from "react";
import type { SearchResult } from "../../stores/search-store.js";

interface SearchResultsProps {
  readonly results: readonly SearchResult[];
  readonly total: number;
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function truncateText(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen - 3)}...`;
}

function formatScore(score: number): string {
  return score.toFixed(2);
}

function formatLineRange(start: number, end: number): string {
  if (start === end) return `L${start}`;
  return `L${start}-${end}`;
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
        <text>{"  SCORE  LINES      PATH                           CHUNK"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  -----  ---------  -----------------------------  --------------------------------"}</text>
      </box>

      {/* Result rows */}
      <scrollbox flexGrow={1} width="100%">
        {results.map((result, i) => {
          const isSelected = i === selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const score = formatScore(result.score).padEnd(5);
          const lines = formatLineRange(result.line_start, result.line_end).padEnd(9);
          const path = truncateText(result.path, 29).padEnd(29);
          const chunk = truncateText(result.chunk_text, 32);

          return (
            <box key={`${result.path}:${result.chunk_index}`} height={1} width="100%">
              <text>
                {`${prefix}${score}  ${lines}  ${path}  ${chunk}`}
              </text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
