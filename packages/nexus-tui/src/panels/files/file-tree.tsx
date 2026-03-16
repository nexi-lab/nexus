/**
 * Recursive file tree with lazy-expand for directories.
 * Flattens the visible tree for virtual scrolling.
 */

import React, { useEffect, useMemo } from "react";
import { useFilesStore, type TreeNode } from "../../stores/files-store.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { FileTreeNode } from "./file-tree-node.js";
import { Spinner } from "../../shared/components/spinner.js";
import { ScrollIndicator } from "../../shared/components/scroll-indicator.js";

export function FileTree(): React.ReactNode {
  const client = useApi();
  const treeNodes = useFilesStore((s) => s.treeNodes);
  const currentPath = useFilesStore((s) => s.currentPath);
  const selectedIndex = useFilesStore((s) => s.selectedIndex);
  const expandNode = useFilesStore((s) => s.expandNode);

  // Initialize root on mount
  useEffect(() => {
    if (client && !treeNodes.has(currentPath)) {
      expandNode(currentPath, client);
    }
  }, [client, currentPath, treeNodes, expandNode]);

  // Flatten visible tree nodes for rendering
  const visibleNodes = useMemo(() => {
    return flattenVisibleNodes(currentPath, treeNodes);
  }, [currentPath, treeNodes]);

  if (!client) {
    return <text>No connection configured</text>;
  }

  if (visibleNodes.length === 0) {
    const rootNode = treeNodes.get(currentPath);
    if (rootNode?.loading) {
      return <Spinner label="Loading..." />;
    }
    return <text>Empty directory</text>;
  }

  return (
    <ScrollIndicator selectedIndex={selectedIndex} totalItems={visibleNodes.length} visibleItems={20}>
      <scrollbox height="100%" width="100%">
        {visibleNodes.map((node, index) => (
          <FileTreeNode
            key={node.path}
            node={node}
            selected={index === selectedIndex}
          />
        ))}
      </scrollbox>
    </ScrollIndicator>
  );
}

/** Flatten tree into ordered list of visible nodes (expanded directories show children). */
function flattenVisibleNodes(
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
    }
  }

  walk(rootPath);
  return result;
}
