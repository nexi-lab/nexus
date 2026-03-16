/**
 * File data store with SWR caching, tree state, and preview state.
 *
 * @see Issue #3102 — LRU cache (Decision 7A), AbortController (Decisions 3A/14A),
 *      sortFileItems helper (Decision 6A), cursor pagination + infinite scroll.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";
import { categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";
import { LruCache } from "../shared/utils/lru-cache.js";

// =============================================================================
// Types
// =============================================================================

export interface FileItem {
  readonly name: string;
  readonly path: string;
  readonly isDirectory: boolean;
  readonly size: number;
  readonly modifiedAt: string | null;
  readonly etag: string | null;
  readonly mimeType: string | null;
  readonly version: number | null;
  readonly owner: string | null;
  readonly permissions: string | null;
  readonly zoneId: string | null;
}

export interface TreeNode {
  readonly path: string;
  readonly name: string;
  readonly isDirectory: boolean;
  readonly expanded: boolean;
  readonly children: readonly string[];
  readonly loading: boolean;
  readonly depth: number;
  readonly size: number;
  /** Opaque cursor for fetching the next page of children. */
  readonly nextCursor: string | null;
  /** Whether more children are available beyond what's loaded. */
  readonly hasMore: boolean;
  /** Whether a "load more" fetch is currently in flight. */
  readonly loadingMore: boolean;
}

/** Paginated list response from the server. */
interface PaginatedListResponse {
  readonly items: readonly FileItem[];
  readonly has_more: boolean;
  readonly next_cursor: string | null;
}

// =============================================================================
// Helpers (Decision 6A: extract sort)
// =============================================================================

/** Sort file items: directories first, then alphabetical by name. */
function sortFileItems(items: readonly FileItem[]): FileItem[] {
  return [...items].sort((a, b) => {
    if (a.isDirectory !== b.isDirectory) return a.isDirectory ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

// =============================================================================
// LRU cache for file listings (Decision 7A: reuse shared LruCache)
// =============================================================================

const CACHE_TTL_MS = 30_000;
const fileCache = new LruCache<readonly FileItem[]>(200);

// =============================================================================
// Per-path AbortController tracking (Decisions 3A + 14A)
// =============================================================================

const inFlightControllers = new Map<string, AbortController>();

function abortForPath(path: string): void {
  inFlightControllers.get(path)?.abort();
  inFlightControllers.delete(path);
}

function controllerForPath(path: string): AbortController {
  abortForPath(path);
  const controller = new AbortController();
  inFlightControllers.set(path, controller);
  return controller;
}

/** Abort all in-flight requests (called on panel unmount). */
function abortAllInFlight(): void {
  for (const controller of inFlightControllers.values()) {
    controller.abort();
  }
  inFlightControllers.clear();
}

// =============================================================================
// Store
// =============================================================================

export interface FilesState {
  // File list cache (external LruCache — store only exposes current path data)
  readonly currentPath: string;
  readonly selectedIndex: number;

  // Revision counter — bumped whenever fileCache changes, so selectors re-fire
  readonly fileCacheRevision: number;

  // Tree state
  readonly treeNodes: ReadonlyMap<string, TreeNode>;
  readonly focusPane: "tree" | "preview";

  // Preview state
  readonly previewPath: string | null;
  readonly previewContent: string | null;
  readonly previewLoading: boolean;

  // Selection state
  readonly selectedPaths: ReadonlySet<string>;
  readonly visualModeAnchor: number | null;

  // Clipboard state
  readonly clipboard: { readonly paths: readonly string[]; readonly operation: "copy" | "cut" } | null;

  // Paste progress
  readonly pasteProgress: { readonly total: number; readonly completed: number; readonly failed: number } | null;

  // Error
  readonly error: string | null;

  // Actions
  readonly setCurrentPath: (path: string) => void;
  readonly setSelectedIndex: (index: number) => void;
  readonly setFocusPane: (pane: "tree" | "preview") => void;
  readonly fetchFiles: (path: string, client: FetchClient) => Promise<void>;
  readonly fetchPreview: (path: string, client: FetchClient) => Promise<void>;
  readonly expandNode: (path: string, client: FetchClient) => Promise<void>;
  readonly collapseNode: (path: string) => void;
  readonly toggleNode: (path: string, client: FetchClient) => Promise<void>;
  readonly loadMoreChildren: (path: string, client: FetchClient) => Promise<void>;
  readonly invalidate: (path: string) => void;
  readonly writeFile: (path: string, content: string, client: FetchClient) => Promise<void>;
  readonly deleteFile: (path: string, client: FetchClient) => Promise<void>;
  readonly mkdirFile: (path: string, client: FetchClient) => Promise<void>;
  readonly renameFile: (oldPath: string, newPath: string, client: FetchClient) => Promise<void>;
  readonly getCachedFiles: (path: string) => readonly FileItem[] | undefined;
  readonly abortAllInFlight: () => void;

  // Selection actions
  readonly toggleSelect: (path: string) => void;
  readonly clearSelection: () => void;
  readonly enterVisualMode: (anchorIndex: number) => void;
  readonly exitVisualMode: () => void;

  // Clipboard actions
  readonly yankToClipboard: (paths: readonly string[]) => void;
  readonly cutToClipboard: (paths: readonly string[]) => void;
  readonly clearClipboard: () => void;

  // Paste action (async with progress)
  readonly pasteFiles: (destinationDir: string, client: FetchClient) => Promise<void>;
}

const SOURCE = "files";

// =============================================================================
// Derived helper (pure function)
// =============================================================================

/**
 * Compute the effective selection: union of manually toggled selectedPaths
 * and the visual-mode range (if active).
 */
export function getEffectiveSelection(
  selectedPaths: ReadonlySet<string>,
  visualModeAnchor: number | null,
  currentIndex: number,
  visibleNodes: readonly string[],
): Set<string> {
  const result = new Set(selectedPaths);

  if (visualModeAnchor !== null && visibleNodes.length > 0) {
    const lo = Math.max(0, Math.min(visualModeAnchor, currentIndex));
    const hi = Math.min(visibleNodes.length - 1, Math.max(visualModeAnchor, currentIndex));
    for (let i = lo; i <= hi; i++) {
      result.add(visibleNodes[i]!);
    }
  }

  return result;
}

/** Expose fileCache for external access (e.g. getCachedFiles selector). */
export { fileCache as _fileCache };

export const useFilesStore = create<FilesState>((set, get) => ({
  currentPath: "/",
  selectedIndex: 0,
  fileCacheRevision: 0,
  treeNodes: new Map(),
  focusPane: "tree",
  previewPath: null,
  previewContent: null,
  previewLoading: false,
  selectedPaths: new Set(),
  visualModeAnchor: null,
  clipboard: null,
  pasteProgress: null,
  error: null,

  setCurrentPath: (path) => set({ currentPath: path, selectedIndex: 0 }),

  setSelectedIndex: (index) => set({ selectedIndex: index }),

  setFocusPane: (pane) => set({ focusPane: pane }),

  getCachedFiles: (path) => {
    const entry = fileCache.get(path);
    if (!entry) return undefined;
    if (Date.now() - entry.fetchedAt > CACHE_TTL_MS) return undefined;
    return entry.data;
  },

  abortAllInFlight,

  fetchFiles: async (path, client) => {
    // Check SWR cache
    const cached = fileCache.get(path);
    if (cached && Date.now() - cached.fetchedAt < CACHE_TTL_MS) {
      return;
    }

    const controller = controllerForPath(`list:${path}`);
    try {
      const response = await client.get<PaginatedListResponse>(
        `/api/v2/files/list?path=${encodeURIComponent(path)}&limit=200`,
        { signal: controller.signal },
      );

      const items = response.items ?? [];
      const sorted = sortFileItems(items);
      fileCache.set(path, { data: sorted, fetchedAt: Date.now() });
      set({ fileCacheRevision: get().fileCacheRevision + 1, error: null });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      const message = err instanceof Error ? err.message : "Failed to fetch files";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    } finally {
      inFlightControllers.delete(`list:${path}`);
    }
  },

  fetchPreview: async (path, client) => {
    set({ previewPath: path, previewLoading: true, previewContent: null });

    const controller = controllerForPath(`preview:${path}`);
    try {
      const response = await client.get<{ content: string }>(
        `/api/v2/files/read?path=${encodeURIComponent(path)}`,
        { signal: controller.signal },
      );
      set({ previewContent: response.content ?? "", previewLoading: false });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      const message = err instanceof Error ? err.message : "Failed to fetch preview";
      set({
        previewContent: null,
        previewLoading: false,
        error: message,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    } finally {
      inFlightControllers.delete(`preview:${path}`);
    }
  },

  expandNode: async (path, client) => {
    const nodes = get().treeNodes;
    const existing = nodes.get(path);

    if (existing?.expanded) return;

    // Abort any in-flight expand for this path (Decision 14A)
    const controller = controllerForPath(`expand:${path}`);

    // Mark as loading
    const loadingNodes = new Map(nodes);
    loadingNodes.set(path, {
      ...(existing ?? {
        path, name: path.split("/").pop() ?? path, isDirectory: true,
        children: [], depth: 0, size: 0, nextCursor: null, hasMore: false, loadingMore: false,
      }),
      expanded: true,
      loading: true,
    });
    set({ treeNodes: loadingNodes });

    try {
      const response = await client.get<PaginatedListResponse>(
        `/api/v2/files/list?path=${encodeURIComponent(path)}&limit=200`,
        { signal: controller.signal },
      );

      const items = response.items ?? [];
      const sorted = sortFileItems(items);

      const parentDepth = existing?.depth ?? 0;
      const updatedNodes = new Map(get().treeNodes);

      // Update parent node with pagination state
      updatedNodes.set(path, {
        ...updatedNodes.get(path)!,
        expanded: true,
        loading: false,
        children: sorted.map((item) => item.path),
        nextCursor: response.next_cursor ?? null,
        hasMore: response.has_more ?? false,
        loadingMore: false,
      });

      // Add child nodes
      for (const item of sorted) {
        if (!updatedNodes.has(item.path)) {
          updatedNodes.set(item.path, {
            path: item.path,
            name: item.name,
            isDirectory: item.isDirectory,
            expanded: false,
            children: [],
            loading: false,
            depth: parentDepth + 1,
            size: item.size,
            nextCursor: null,
            hasMore: false,
            loadingMore: false,
          });
        }
      }

      set({ treeNodes: updatedNodes, error: null });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      // Revert loading state
      const revertNodes = new Map(get().treeNodes);
      const node = revertNodes.get(path);
      if (node) {
        revertNodes.set(path, { ...node, loading: false, expanded: false });
      }
      const message = err instanceof Error ? err.message : "Failed to expand directory";
      set({
        treeNodes: revertNodes,
        error: message,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    } finally {
      inFlightControllers.delete(`expand:${path}`);
    }
  },

  loadMoreChildren: async (path, client) => {
    const nodes = get().treeNodes;
    const parentNode = nodes.get(path);

    if (!parentNode || !parentNode.hasMore || !parentNode.nextCursor || parentNode.loadingMore) {
      return;
    }

    const controller = controllerForPath(`more:${path}`);

    // Mark as loading more
    const loadingNodes = new Map(nodes);
    loadingNodes.set(path, { ...parentNode, loadingMore: true });
    set({ treeNodes: loadingNodes });

    try {
      const response = await client.get<PaginatedListResponse>(
        `/api/v2/files/list?path=${encodeURIComponent(path)}&limit=200&cursor=${encodeURIComponent(parentNode.nextCursor)}`,
        { signal: controller.signal },
      );

      const items = response.items ?? [];
      const sorted = sortFileItems(items);

      const updatedNodes = new Map(get().treeNodes);
      const currentParent = updatedNodes.get(path);
      if (!currentParent) return;

      // Append new children to existing children
      const newChildPaths = sorted.map((item) => item.path);
      updatedNodes.set(path, {
        ...currentParent,
        children: [...currentParent.children, ...newChildPaths],
        nextCursor: response.next_cursor ?? null,
        hasMore: response.has_more ?? false,
        loadingMore: false,
      });

      // Add new child nodes
      for (const item of sorted) {
        if (!updatedNodes.has(item.path)) {
          updatedNodes.set(item.path, {
            path: item.path,
            name: item.name,
            isDirectory: item.isDirectory,
            expanded: false,
            children: [],
            loading: false,
            depth: currentParent.depth + 1,
            size: item.size,
            nextCursor: null,
            hasMore: false,
            loadingMore: false,
          });
        }
      }

      set({ treeNodes: updatedNodes, error: null });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      // Revert loadingMore state
      const revertNodes = new Map(get().treeNodes);
      const node = revertNodes.get(path);
      if (node) {
        revertNodes.set(path, { ...node, loadingMore: false });
      }
      const message = err instanceof Error ? err.message : "Failed to load more files";
      set({ treeNodes: revertNodes, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    } finally {
      inFlightControllers.delete(`more:${path}`);
    }
  },

  collapseNode: (path) => {
    // Abort any in-flight expand/loadMore for this path (Decision 14A)
    abortForPath(`expand:${path}`);
    abortForPath(`more:${path}`);

    const nodes = new Map(get().treeNodes);
    const node = nodes.get(path);
    if (node) {
      nodes.set(path, { ...node, expanded: false });
      set({ treeNodes: nodes });
    }
  },

  toggleNode: async (path, client) => {
    const node = get().treeNodes.get(path);
    if (node?.expanded) {
      get().collapseNode(path);
    } else {
      await get().expandNode(path, client);
    }
  },

  invalidate: (path) => {
    fileCache.delete(path);
    set({ fileCacheRevision: get().fileCacheRevision + 1 });
  },

  writeFile: async (path, content, client) => {
    set({ error: null });
    try {
      await client.post("/api/v2/files/write", { path, content });
      get().invalidate(path.split("/").slice(0, -1).join("/") || "/");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to write file";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  deleteFile: async (path, client) => {
    set({ error: null });
    try {
      await client.delete(`/api/v2/files/delete?path=${encodeURIComponent(path)}`);
      const parentPath = path.split("/").slice(0, -1).join("/") || "/";
      get().invalidate(parentPath);
      await get().fetchFiles(parentPath, client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to delete file";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  mkdirFile: async (path, client) => {
    set({ error: null });
    try {
      await client.post("/api/v2/files/mkdir", { path });
      const parentPath = path.split("/").slice(0, -1).join("/") || "/";
      get().invalidate(parentPath);
      await get().fetchFiles(parentPath, client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to create directory";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  renameFile: async (oldPath, newPath, client) => {
    // Atomic rename via dedicated endpoint (Decision 8A) — O(1) metadata-only operation.
    set({ error: null });
    try {
      await client.post("/api/v2/files/rename", { source: oldPath, destination: newPath });
      const parentPath = oldPath.split("/").slice(0, -1).join("/") || "/";
      get().invalidate(parentPath);
      // Also invalidate destination parent if different
      const destParent = newPath.split("/").slice(0, -1).join("/") || "/";
      if (destParent !== parentPath) get().invalidate(destParent);
      await get().fetchFiles(parentPath, client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to rename file";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  // Selection actions

  toggleSelect: (path) => {
    const next = new Set(get().selectedPaths);
    if (next.has(path)) {
      next.delete(path);
    } else {
      next.add(path);
    }
    set({ selectedPaths: next });
  },

  clearSelection: () => set({ selectedPaths: new Set(), visualModeAnchor: null }),

  enterVisualMode: (anchorIndex) => set({ visualModeAnchor: anchorIndex }),

  exitVisualMode: () => set({ visualModeAnchor: null }),

  // Clipboard actions

  yankToClipboard: (paths) => set({ clipboard: { paths: [...paths], operation: "copy" } }),

  cutToClipboard: (paths) => set({ clipboard: { paths: [...paths], operation: "cut" } }),

  clearClipboard: () => set({ clipboard: null }),

  // Paste action with progress tracking

  pasteFiles: async (destinationDir, client) => {
    const { clipboard } = get();
    if (!clipboard || clipboard.paths.length === 0) return;

    const total = clipboard.paths.length;
    set({ pasteProgress: { total, completed: 0, failed: 0 }, error: null });

    const operation = clipboard.operation;
    let completed = 0;
    let failed = 0;

    for (const srcPath of clipboard.paths) {
      const fileName = srcPath.split("/").pop() ?? srcPath;
      const destPath = destinationDir === "/" ? `/${fileName}` : `${destinationDir}/${fileName}`;
      try {
        if (operation === "copy") {
          await client.post("/api/v2/files/copy", { source: srcPath, destination: destPath });
        } else {
          await client.post("/api/v2/files/rename", { source: srcPath, destination: destPath });
        }
        completed++;
      } catch {
        failed++;
      }
      set({ pasteProgress: { total, completed, failed } });
    }

    // Clear clipboard and progress, invalidate caches
    set({ clipboard: null });
    get().invalidate(destinationDir);

    // Also invalidate source parents for cut operations
    if (operation === "cut") {
      const sourceParents = new Set(
        clipboard.paths.map((p) => p.split("/").slice(0, -1).join("/") || "/"),
      );
      for (const parent of sourceParents) {
        get().invalidate(parent);
      }
    }

    await get().fetchFiles(destinationDir, client);

    // Clear progress after a short delay so the user sees the completion state
    const finalCompleted = completed;
    const finalFailed = failed;
    setTimeout(() => {
      const p = get().pasteProgress;
      if (p && p.completed === finalCompleted && p.failed === finalFailed) {
        set({ pasteProgress: null });
      }
    }, 2000);

    if (failed > 0) {
      const message = `Paste: ${failed} of ${total} operations failed`;
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: "server", source: SOURCE });
    }
  },
}));
