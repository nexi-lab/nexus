/**
 * Single tree node row: indent + expand/collapse icon + file/folder icon + name + size.
 *
 * Wrapped with React.memo — re-renders only when node, selected, or marked changes.
 * Shows selection checkmark for multi-select (Decision 3A).
 * @see Issue #3102, Decisions 4A + 5A
 */

import React from "react";
import type { TreeNode } from "../../stores/files-store.js";
import { formatSize } from "../../shared/utils/format-size.js";

interface FileTreeNodeProps {
  readonly node: TreeNode;
  readonly selected: boolean;
  /** Whether this node is in the current multi-selection set. */
  readonly marked: boolean;
}

export const FileTreeNode = React.memo(function FileTreeNode({ node, selected, marked }: FileTreeNodeProps): React.ReactNode {
  const indent = "  ".repeat(node.depth);
  const cursor = selected ? "▸ " : "  ";
  const check = marked ? "✓ " : "  ";

  let expandIcon = "  ";
  if (node.isDirectory) {
    if (node.loading) {
      expandIcon = "⟳ ";
    } else if (node.expanded) {
      expandIcon = "▾ ";
    } else {
      expandIcon = "▸ ";
    }
  }

  const fileIcon = node.isDirectory ? "📁" : "📄";
  const sizeSuffix = !node.isDirectory && node.size > 0 ? ` (${formatSize(node.size)})` : "";

  return (
    <box height={1} width="100%">
      <text>{`${cursor}${check}${indent}${expandIcon}${fileIcon} ${node.name}${sizeSuffix}`}</text>
    </box>
  );
});
