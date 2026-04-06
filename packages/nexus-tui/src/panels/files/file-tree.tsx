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

import { createEffect, createMemo, Show } from "solid-js";
import type { JSX } from "solid-js";
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

export function FileTree(props: FileTreeProps): JSX.Element {
  const client = useApi();
  const setSelectedIndex = useFilesStore((s) => s.setSelectedIndex);
  const toggleNode = useFilesStore((s) => s.toggleNode);
  const fetchPreview = useFilesStore((s) => s.fetchPreview);
  const expandNode = useFilesStore((s) => s.expandNode);
  const loadMoreChildren = useFilesStore((s) => s.loadMoreChildren);

  const filterQuery = () => props.filterQuery ?? "";
  const effectiveSelection = () => props.effectiveSelection;

  // Reactive store accessors (direct reads via jsx:preserve)
  const revision = () => useFilesStore((s) => s.fileCacheRevision);
  const currentPath = () => useFilesStore((s) => s.currentPath);
  const selectedIndex = () => useFilesStore((s) => s.selectedIndex);
  const treeNodes = () => useFilesStore((s) => s.treeNodes);

  // Initialize root on mount
  createEffect(() => {
    void revision(); // trigger when store updates
    const path = currentPath();
    const nodes = treeNodes();
    if (client && !nodes.has(path)) {
      expandNode(path, client);
    }
  });

  // Flatten visible tree nodes, then apply filter (Decision 13A).
  // Inserts "load more" sentinel nodes at the end of paginated directories.
  const visibleNodes = createMemo(() => {
    void revision(); // trigger re-evaluation when store updates
    const all = flattenVisibleNodes(currentPath(), treeNodes());
    const fq = filterQuery();
    if (!fq) return all;
    const lowerQuery = fq.toLowerCase();
    return all.filter((node) => fuzzyMatch(node.name.toLowerCase(), lowerQuery));
  });

  // Auto-load more: when the selected node is a "load more" sentinel, trigger fetch
  createEffect(() => {
    if (!client) return;
    const nodes = visibleNodes();
    const idx = selectedIndex();
    const selectedNode = nodes[idx];
    if (!selectedNode || !selectedNode.path.endsWith(LOAD_MORE_SENTINEL)) return;

    // Extract the parent path from the sentinel node
    const parentPath = selectedNode.path.slice(0, -(LOAD_MORE_SENTINEL.length + 1));
    const parentNode = treeNodes().get(parentPath);
    if (parentNode?.hasMore && !parentNode.loadingMore) {
      loadMoreChildren(parentPath, client);
    }
  });

  // Stable render callback for VirtualList (avoids inline closure per-render)
  const renderNode = (node: TreeNode, index: number) => (
      <FileTreeNode
        node={node}
        selected={index === selectedIndex()}
        marked={effectiveSelection()?.has(node.path) ?? false}
      />
    );

  return (
    <Show when={client} fallback={<text>No connection configured</text>}>
      <Show
        when={visibleNodes().length > 0}
        fallback={
          <Show when={treeNodes().get(currentPath())?.loading} fallback={<text>{filterQuery() ? "No matches" : "Empty directory"}</text>}>
            <Spinner label="Loading..." />
          </Show>
        }
      >
        <ScrollIndicator selectedIndex={selectedIndex()} totalItems={visibleNodes().length} visibleItems={VIEWPORT_HEIGHT}>
          <VirtualList
            items={visibleNodes()}
            renderItem={renderNode}
            viewportHeight={VIEWPORT_HEIGHT}
            selectedIndex={selectedIndex()}
            overscan={5}
            onSelect={(index) => {
              setSelectedIndex(index);
              const node = visibleNodes()[index];
              if (!node || node.path.endsWith(LOAD_MORE_SENTINEL) || !client) return;
              if (node.isDirectory) {
                void toggleNode(node.path, client);
              } else {
                void fetchPreview(node.path, client);
              }
            }}
          />
        </ScrollIndicator>
      </Show>
    </Show>
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
