/**
 * Single tree node row: indent + expand/collapse icon + file/folder icon + name.
 */

import React from "react";
import type { TreeNode } from "../../stores/files-store.js";

interface FileTreeNodeProps {
  readonly node: TreeNode;
  readonly selected: boolean;
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

  return (
    <box height={1} width="100%">
      <text>{`${prefix}${indent}${expandIcon}${fileIcon} ${node.name}`}</text>
    </box>
  );
}
