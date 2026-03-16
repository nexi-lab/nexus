/**
 * Search results list: path, chunk_text (truncated), score, line range.
 */

import React from "react";
import type { SearchResult } from "../../stores/search-store.js";
import { statusColor } from "../../shared/theme.js";
import { EmptyState } from "../../shared/components/empty-state.js";

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

function scoreColor(score: number): string {
  if (score > 0.7) return statusColor.healthy;
  if (score >= 0.4) return statusColor.warning;
  return statusColor.dim;
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
      <EmptyState
        message="No results."
        hint="Try a different query or search mode (m to cycle: KW → SEM → HYB)."
      />
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
              <text>{prefix}</text>
              <text foregroundColor={scoreColor(result.score)}>{score}</text>
              <text>{`  ${lines}  ${path}  ${chunk}`}</text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
