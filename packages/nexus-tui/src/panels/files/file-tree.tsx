/**
 * Recursive file tree with lazy-expand for directories.
 * Flattens the visible tree, then uses VirtualList for windowed rendering.
 * Auto-loads more children when the user scrolls near the end of a paginated directory.
 *
 * Supports client-side fuzzy filtering (Decision 4A, 13A)
 * and rendering multi-selection state (Decision 3A).
 *
 * @see Issue #3102, Decisions 1A (virtualization) + 4A (React.memo on children)
 */

import React, { useCallback, useEffect, useMemo } from "react";
import { useFilesStore, type TreeNode } from "../../stores/files-store.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { FileTreeNode } from "./file-tree-node.js";
import { Spinner } from "../../shared/components/spinner.js";
import { ScrollIndicator } from "../../shared/components/scroll-indicator.js";
import { VirtualList } from "../../shared/components/virtual-list.js";

const VIEWPORT_HEIGHT = 20;

/** Sentinel value used to mark "load more" placeholder nodes in the flattened list. */
export const LOAD_MORE_SENTINEL = "__load_more__";

/** A synthetic TreeNode representing a "load more" placeholder. */
export function makeLoadMoreNode(parentPath: string, depth: number, loading: boolean): TreeNode {
  return {
    path: `${parentPath}/${LOAD_MORE_SENTINEL}`,
    name: loading ? "Loading more..." : "▼ Load more...",
    isDirectory: false,
    expanded: false,
    children: [],
    loading: false,
    depth,
    size: 0,
    nextCursor: null,
    hasMore: false,
    loadingMore: false,
  };
}

interface FileTreeProps {
  /** Client-side fuzzy filter query. Empty string = no filter. */
  readonly filterQuery?: string;
  /** Set of currently selected file paths (for multi-select rendering). */
  readonly effectiveSelection?: ReadonlySet<string>;
}

export function FileTree({ filterQuery = "", effectiveSelection }: FileTreeProps): React.ReactNode {
  const client = useApi();
  const treeNodes = useFilesStore((s) => s.treeNodes);
  const currentPath = useFilesStore((s) => s.currentPath);
  const selectedIndex = useFilesStore((s) => s.selectedIndex);
  const setSelectedIndex = useFilesStore((s) => s.setSelectedIndex);
  const toggleNode = useFilesStore((s) => s.toggleNode);
  const fetchPreview = useFilesStore((s) => s.fetchPreview);
  const expandNode = useFilesStore((s) => s.expandNode);
  const loadMoreChildren = useFilesStore((s) => s.loadMoreChildren);

  // Initialize root on mount
  useEffect(() => {
    if (client && !treeNodes.has(currentPath)) {
      expandNode(currentPath, client);
    }
  }, [client, currentPath, treeNodes, expandNode]);

  // Flatten visible tree nodes, then apply filter (Decision 13A).
  // Inserts "load more" sentinel nodes at the end of paginated directories.
  const visibleNodes = useMemo(() => {
    const all = flattenVisibleNodes(currentPath, treeNodes);
    if (!filterQuery) return all;
    const lowerQuery = filterQuery.toLowerCase();
    return all.filter((node) => fuzzyMatch(node.name.toLowerCase(), lowerQuery));
  }, [currentPath, treeNodes, filterQuery]);

  // Auto-load more: when the selected node is a "load more" sentinel, trigger fetch
  useEffect(() => {
    if (!client) return;
    const selectedNode = visibleNodes[selectedIndex];
    if (!selectedNode || !selectedNode.path.endsWith(LOAD_MORE_SENTINEL)) return;

    // Extract the parent path from the sentinel node
    const parentPath = selectedNode.path.slice(0, -(LOAD_MORE_SENTINEL.length + 1));
    const parentNode = treeNodes.get(parentPath);
    if (parentNode?.hasMore && !parentNode.loadingMore) {
      loadMoreChildren(parentPath, client);
    }
  }, [client, selectedIndex, visibleNodes, treeNodes, loadMoreChildren]);

  // Stable render callback for VirtualList (avoids inline closure per-render)
  const renderNode = useCallback(
    (node: TreeNode, index: number) => (
      <FileTreeNode
        key={node.path}
        node={node}
        selected={index === selectedIndex}
        marked={effectiveSelection?.has(node.path) ?? false}
      />
    ),
    [selectedIndex, effectiveSelection],
  );

  if (!client) {
    return <text>No connection configured</text>;
  }

  if (visibleNodes.length === 0) {
    const rootNode = treeNodes.get(currentPath);
    if (rootNode?.loading) {
      return <Spinner label="Loading..." />;
    }
    return <text>{filterQuery ? "No matches" : "Empty directory"}</text>;
  }

  return (
    <ScrollIndicator selectedIndex={selectedIndex} totalItems={visibleNodes.length} visibleItems={VIEWPORT_HEIGHT}>
      <VirtualList
        items={visibleNodes}
        renderItem={renderNode}
        viewportHeight={VIEWPORT_HEIGHT}
        selectedIndex={selectedIndex}
        overscan={5}
        onSelect={(index) => {
          setSelectedIndex(index);
          const node = visibleNodes[index];
          if (!node || node.path.endsWith(LOAD_MORE_SENTINEL) || !client) return;
          if (node.isDirectory) {
            void toggleNode(node.path, client);
          } else {
            void fetchPreview(node.path, client);
          }
        }}
      />
    </ScrollIndicator>
  );
}

/** Simple fuzzy match: all characters of query appear in order in target. */
function fuzzyMatch(target: string, query: string): boolean {
  let qi = 0;
  for (let ti = 0; ti < target.length && qi < query.length; ti++) {
    if (target[ti] === query[qi]) qi++;
  }
  return qi === query.length;
}

/**
 * Flatten tree into ordered list of visible nodes (expanded directories show children).
 * Appends a synthetic "load more" node after the last child of any directory with hasMore.
 */
export function flattenVisibleNodes(
  rootPath: string,
  nodes: ReadonlyMap<string, TreeNode>,
): readonly TreeNode[] {
  const result: TreeNode[] = [];
  const root = nodes.get(rootPath);
  if (!root) return result;

  function walk(nodePath: string): void {
    const node = nodes.get(nodePath);
    if (!node) return;

    // Don't include the root itself — only its children
    if (nodePath !== rootPath) {
      result.push(node);
    }

    if (node.expanded) {
      for (const childPath of node.children) {
        walk(childPath);
      }

      // If this directory has more pages, show a "load more" placeholder
      if (node.hasMore) {
        result.push(makeLoadMoreNode(nodePath, node.depth + 1, node.loadingMore));
      }
    }
  }

  walk(rootPath);
  return result;
}
