/**
 * Recursive file tree with lazy-expand for directories.
 * Flattens the visible tree for virtual scrolling.
 *
 * Supports client-side fuzzy filtering (Decision 4A, 13A)
 * and rendering multi-selection state (Decision 3A).
 */

import React, { useEffect, useMemo } from "react";
import { useFilesStore, type TreeNode } from "../../stores/files-store.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { FileTreeNode } from "./file-tree-node.js";
import { Spinner } from "../../shared/components/spinner.js";
import { ScrollIndicator } from "../../shared/components/scroll-indicator.js";

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
  const expandNode = useFilesStore((s) => s.expandNode);

  // Initialize root on mount
  useEffect(() => {
    if (client && !treeNodes.has(currentPath)) {
      expandNode(currentPath, client);
    }
  }, [client, currentPath, treeNodes, expandNode]);

  // Flatten visible tree nodes, then apply filter (Decision 13A)
  const visibleNodes = useMemo(() => {
    const all = flattenVisibleNodes(currentPath, treeNodes);
    if (!filterQuery) return all;
    const lowerQuery = filterQuery.toLowerCase();
    return all.filter((node) => fuzzyMatch(node.name.toLowerCase(), lowerQuery));
  }, [currentPath, treeNodes, filterQuery]);

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
    <ScrollIndicator selectedIndex={selectedIndex} totalItems={visibleNodes.length} visibleItems={20}>
      <scrollbox height="100%" width="100%">
        {visibleNodes.map((node, index) => (
          <FileTreeNode
            key={node.path}
            node={node}
            selected={index === selectedIndex}
            marked={effectiveSelection?.has(node.path) ?? false}
          />
        ))}
      </scrollbox>
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
