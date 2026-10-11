import type { JSX } from "solid-js";
/**
 * Search results list with scores, zone and chunk text.
 */

import type { SearchResult } from "../../stores/search-store.js";
import { textStyle } from "../../shared/text-style.js";
import { statusColor } from "../../shared/theme.js";
import { VirtualList } from "../../shared/components/virtual-list.js";
import { truncateText } from "../../shared/utils/format-text.js";

const VIEWPORT_HEIGHT = 20;

interface SearchResultsProps {
  readonly results: readonly SearchResult[];
  readonly total: number;
  readonly selectedIndex: number;
  readonly loading: boolean;
  readonly error?: string | null;
}

function formatScore(score: number): string {
  return score.toFixed(2);
}

function scoreColor(score: number): string {
  if (score > 0.7) return statusColor.healthy;
  if (score >= 0.4) return statusColor.warning;
  return statusColor.dim;
}

function formatScoreBreakdown(result: SearchResult): string {
  const parts: string[] = [];
  if (result.keyword_score != null) parts.push(`bm25:${result.keyword_score.toFixed(2)}`);
  if (result.vector_score != null) parts.push(`vec:${result.vector_score.toFixed(2)}`);
  return parts.length > 0 ? parts.join(" ") : "";
}

export function SearchResults(props: SearchResultsProps): JSX.Element {
  const renderResult = (result: SearchResult, i: number) => {
    const isSelected = i === props.selectedIndex;
    const prefix = isSelected ? "> " : "  ";
    const score = formatScore(result.score).padEnd(5);
    const path = truncateText(result.path, 29).padEnd(29);
    const breakdown = formatScoreBreakdown(result);
    const chunk = truncateText(result.chunk_text.replace(/\n/g, " "), 30);

    return (
      <box key={`${result.zone_id}:${result.path}:${result.chunk_index}`} height={1} width="100%">
        <text>
          <span>{prefix}</span>
          <span style={textStyle({ fg: scoreColor(result.score) })}>{score}</span>
          <span>{`  ${result.zone_id}  ${path}  `}</span>
          <span style={textStyle({ dim: true })}>{breakdown ? `[${breakdown}]  ` : ""}</span>
          <span>{chunk}</span>
        </text>
      </box>
    );
  };

  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Searching..."
          : props.error && props.results.length === 0
            ? "Search failed"
          : props.results.length === 0
            ? "No results. Try a different query or search mode (m to cycle: KW -> SEM -> HYB)."
            : `Results: ${props.total} found`}
      </text>

      {/* Header */}
      <box height={1} width="100%">
        <text>{"  SCORE  ZONE  PATH                           CHUNK"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  -----  ----  -----------------------------  --------------------------------"}</text>
      </box>

      {/* Result rows */}
      <VirtualList
        items={props.results}
        renderItem={renderResult}
        viewportHeight={VIEWPORT_HEIGHT}
        selectedIndex={props.selectedIndex}
      />
    </box>
  );
}
