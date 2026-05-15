/**
 * Shared pagination bar for lists with cursor-based or offset-based pagination.
 *
 * Shows current page position and provides prev/next hints.
 *
 * @see Issue #3066, Phase E4
 */

import { Show } from "solid-js";
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

export function PaginationBar(props: PaginationBarProps) {
  const pageDisplay = formatPageDisplay(props.currentPage, props.hasMore, props.totalPages);

  return (
    <box height={1} width="100%" flexDirection="row">
      <text style={textStyle({ dim: true })}>
        <Show when={props.hasPrev}>
          <span>
            <span style={textStyle({ fg: statusColor.info })}>{props.prevKey ?? "["}</span>
            <span>{":prev "}</span>
          </span>
        </Show>
        <span>{props.loading ? "Loading..." : pageDisplay}</span>
        <Show when={props.hasMore}>
          <span>
            <span>{" "}</span>
            <span style={textStyle({ fg: statusColor.info })}>{props.nextKey ?? "]"}</span>
            <span>{":next"}</span>
          </span>
        </Show>
      </text>
    </box>
  );
}
