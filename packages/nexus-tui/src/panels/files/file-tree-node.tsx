/**
 * Single tree node row: indent + expand/collapse icon + file/folder icon + name + size.
 *
 * Wrapped in React.memo to avoid unnecessary re-renders during filtering (Decision 13A).
 * Shows selection checkmark for multi-select (Decision 3A).
 */

import React from "react";
import type { TreeNode } from "../../stores/files-store.js";

interface FileTreeNodeProps {
  readonly node: TreeNode;
  readonly selected: boolean;
  /** Whether this node is in the current multi-selection set. */
  readonly marked: boolean;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function FileTreeNodeInner({ node, selected, marked }: FileTreeNodeProps): React.ReactNode {
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
}

export const FileTreeNode = React.memo(FileTreeNodeInner);
