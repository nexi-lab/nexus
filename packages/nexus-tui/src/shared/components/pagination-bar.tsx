/**
 * Shared pagination bar for lists with cursor-based or offset-based pagination.
 *
 * Shows current page position and provides prev/next hints.
 *
 * @see Issue #3066, Phase E4
 */

import React from "react";
import { statusColor } from "../theme.js";
import { textStyle } from "../text-style.js";

interface PaginationBarProps {
  /** Whether there are more items to load (next page available) */
  readonly hasMore: boolean;
  /** Whether there's a previous page */
  readonly hasPrev: boolean;
  /** Current page number (1-based) */
  readonly currentPage: number;
  /** Total pages if known, undefined for cursor-based pagination */
  readonly totalPages?: number;
  /** Key hint for next page. Default: "]" */
  readonly nextKey?: string;
  /** Key hint for previous page. Default: "[" */
  readonly prevKey?: string;
  /** Whether a page is currently loading */
  readonly loading?: boolean;
}

/** Pure function for page display text — exported for testing. */
export function formatPageDisplay(currentPage: number, hasMore: boolean, totalPages?: number): string {
  return totalPages
    ? `Page ${currentPage} of ${totalPages}`
    : `Page ${currentPage}${hasMore ? "+" : ""}`;
}

export function PaginationBar({
  hasMore,
  hasPrev,
  currentPage,
  totalPages,
  nextKey = "]",
  prevKey = "[",
  loading = false,
}: PaginationBarProps): React.ReactNode {
  const pageDisplay = formatPageDisplay(currentPage, hasMore, totalPages);

  return (
    <box height={1} width="100%" flexDirection="row">
      <text style={textStyle({ dim: true })}>
        {hasPrev && (
          <span>
            <span style={textStyle({ fg: statusColor.info })}>{prevKey}</span>
            <span>{":prev "}</span>
          </span>
        )}
        <span>{loading ? "Loading..." : pageDisplay}</span>
        {hasMore && (
          <span>
            <span>{" "}</span>
            <span style={textStyle({ fg: statusColor.info })}>{nextKey}</span>
            <span>{":next"}</span>
          </span>
        )}
      </text>
    </box>
  );
}
