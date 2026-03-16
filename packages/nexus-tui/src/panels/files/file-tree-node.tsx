/**
 * Single tree node row: indent + expand/collapse icon + file/folder icon + name + size.
 */

import React from "react";
import type { TreeNode } from "../../stores/files-store.js";

interface FileTreeNodeProps {
  readonly node: TreeNode;
  readonly selected: boolean;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function FileTreeNode({ node, selected }: FileTreeNodeProps): React.ReactNode {
  const indent = "  ".repeat(node.depth);
  const prefix = selected ? "▸ " : "  ";

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
      <text>{`${prefix}${indent}${expandIcon}${fileIcon} ${node.name}${sizeSuffix}`}</text>
    </box>
  );
}
