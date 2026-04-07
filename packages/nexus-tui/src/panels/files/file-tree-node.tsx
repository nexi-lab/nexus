import type { JSX } from "solid-js";
/**
 * Single tree node row: indent + expand/collapse icon + file/folder icon + name + size.
 *
 * Wrapped with React.memo — re-renders only when node, selected, or marked changes.
 * Shows selection checkmark for multi-select (Decision 3A).
 * @see Issue #3102, Decisions 4A + 5A
 */

import type { TreeNode } from "../../stores/files-store.js";
import { formatSize } from "../../shared/utils/format-size.js";

interface FileTreeNodeProps {
  readonly node: TreeNode;
  readonly selected: boolean;
  /** Whether this node is in the current multi-selection set. */
  readonly marked: boolean;
}

export function FileTreeNode(props: FileTreeNodeProps): JSX.Element {
  const indent = () => "  ".repeat(props.node.depth);
  const cursor = () => props.selected ? "▸ " : "  ";
  const check = () => props.marked ? "✓ " : "  ";

  const expandIcon = () => {
    if (!props.node.isDirectory) return "  ";
    if (props.node.loading) return "⟳ ";
    if (props.node.expanded) return "▾ ";
    return "▸ ";
  };

  const fileIcon = () => props.node.isDirectory ? "📁" : "📄";
  const sizeSuffix = () => !props.node.isDirectory && props.node.size > 0 ? ` (${formatSize(props.node.size)})` : "";

  return (
    <box height={1} width="100%">
      <text>{`${cursor()}${check()}${indent()}${expandIcon()}${fileIcon()} ${props.node.name}${sizeSuffix()}`}</text>
    </box>
  );
}
