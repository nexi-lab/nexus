/**
 * Reusable diff viewer component.
 *
 * Generates a unified diff from oldText/newText and renders it
 * using OpenTUI's built-in <diff> component for syntax-highlighted,
 * scrollable diff display.
 */

import React from "react";

// =============================================================================
// Types
// =============================================================================

interface DiffViewerProps {
  readonly oldText: string;
  readonly newText: string;
  readonly oldLabel?: string;
  readonly newLabel?: string;
  readonly view?: "unified" | "split";
}

// =============================================================================
// Minimal unified diff generator
// =============================================================================

/**
 * Produce a unified diff string from two texts.
 *
 * Uses a simple LCS-based line diff (O(n*m) but fine for typical file sizes
 * viewed in a TUI). Output follows the standard unified diff format that
 * OpenTUI's DiffRenderable can parse.
 */
function generateUnifiedDiff(
  oldText: string,
  newText: string,
  oldLabel: string,
  newLabel: string,
): string {
  const oldLines = oldText.split("\n");
  const newLines = newText.split("\n");

  // Build LCS table
  const m = oldLines.length;
  const n = newLines.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () =>
    new Array<number>(n + 1).fill(0),
  );

  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (oldLines[i - 1] === newLines[j - 1]) {
        dp[i]![j] = dp[i - 1]![j - 1]! + 1;
      } else {
        dp[i]![j] = Math.max(dp[i - 1]![j]!, dp[i]![j - 1]!);
      }
    }
  }

  // Back-trace to produce edit operations
  const ops: Array<{ type: "keep" | "remove" | "add"; line: string }> = [];
  let i = m;
  let j = n;

  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      ops.push({ type: "keep", line: oldLines[i - 1]! });
      i--;
      j--;
    } else if (j > 0 && (i === 0 || dp[i]![j - 1]! >= dp[i - 1]![j]!)) {
      ops.push({ type: "add", line: newLines[j - 1]! });
      j--;
    } else {
      ops.push({ type: "remove", line: oldLines[i - 1]! });
      i--;
    }
  }

  ops.reverse();

  // Group into hunks with 3-line context
  const CONTEXT = 3;
  const hunks: Array<{
    oldStart: number;
    oldCount: number;
    newStart: number;
    newCount: number;
    lines: string[];
  }> = [];

  let oldIdx = 0;
  let newIdx = 0;

  let opIdx = 0;
  while (opIdx < ops.length) {
    // Skip context-only regions until we find a change
    if (ops[opIdx]!.type === "keep") {
      oldIdx++;
      newIdx++;
      opIdx++;
      continue;
    }

    // Start a new hunk with leading context
    const contextStart = Math.max(0, opIdx - CONTEXT);
    let hunkOldStart = oldIdx;
    let hunkNewStart = newIdx;

    // Rewind counters for leading context
    let rewind = opIdx - contextStart;
    hunkOldStart -= rewind;
    hunkNewStart -= rewind;

    const hunkLines: string[] = [];
    let hunkOldCount = 0;
    let hunkNewCount = 0;

    // Add leading context
    for (let c = contextStart; c < opIdx; c++) {
      hunkLines.push(` ${ops[c]!.line}`);
      hunkOldCount++;
      hunkNewCount++;
    }

    // Add changes and trailing context
    let trailingContext = 0;
    while (opIdx < ops.length) {
      const op = ops[opIdx]!;
      if (op.type === "keep") {
        trailingContext++;
        hunkLines.push(` ${op.line}`);
        hunkOldCount++;
        hunkNewCount++;
        if (trailingContext >= CONTEXT * 2) {
          // Enough trailing context, end hunk
          break;
        }
      } else {
        // Reset trailing context counter on any change
        trailingContext = 0;
        if (op.type === "remove") {
          hunkLines.push(`-${op.line}`);
          hunkOldCount++;
        } else {
          hunkLines.push(`+${op.line}`);
          hunkNewCount++;
        }
      }
      opIdx++;
    }

    // Trim excess trailing context to CONTEXT lines
    while (trailingContext > CONTEXT) {
      hunkLines.pop();
      hunkOldCount--;
      hunkNewCount--;
      trailingContext--;
    }

    hunks.push({
      oldStart: hunkOldStart + 1,
      oldCount: hunkOldCount,
      newStart: hunkNewStart + 1,
      newCount: hunkNewCount,
      lines: hunkLines,
    });

    // Advance past remaining keep ops that were consumed
    oldIdx = hunkOldStart + hunkOldCount;
    newIdx = hunkNewStart + hunkNewCount;
  }

  if (hunks.length === 0) {
    return "";
  }

  // Build unified diff output
  const output: string[] = [
    `--- ${oldLabel}`,
    `+++ ${newLabel}`,
  ];

  for (const hunk of hunks) {
    output.push(
      `@@ -${hunk.oldStart},${hunk.oldCount} +${hunk.newStart},${hunk.newCount} @@`,
    );
    output.push(...hunk.lines);
  }

  return output.join("\n");
}

// =============================================================================
// Component
// =============================================================================

export function DiffViewer({
  oldText,
  newText,
  oldLabel = "a",
  newLabel = "b",
  view = "unified",
}: DiffViewerProps): React.ReactNode {
  const diffString = generateUnifiedDiff(oldText, newText, oldLabel, newLabel);

  if (diffString === "") {
    return (
      <box height="100%" width="100%">
        <text>No differences found.</text>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      <diff diff={diffString} view={view} showLineNumbers />
    </box>
  );
}
