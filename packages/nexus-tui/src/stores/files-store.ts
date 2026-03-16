/**
 * File data store with SWR caching, tree state, and preview state.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";
import { categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";

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
}

interface CachedEntry<T> {
  readonly data: T;
  readonly fetchedAt: number;
}

const CACHE_TTL_MS = 30_000;

// =============================================================================
// Store
// =============================================================================

export interface FilesState {
  // File list cache
  readonly fileCache: ReadonlyMap<string, CachedEntry<readonly FileItem[]>>;
  readonly currentPath: string;
  readonly selectedIndex: number;

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
  readonly invalidate: (path: string) => void;
  readonly writeFile: (path: string, content: string, client: FetchClient) => Promise<void>;
  readonly deleteFile: (path: string, client: FetchClient) => Promise<void>;
  readonly mkdirFile: (path: string, client: FetchClient) => Promise<void>;
  readonly renameFile: (oldPath: string, newPath: string, client: FetchClient) => Promise<void>;

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

export const useFilesStore = create<FilesState>((set, get) => ({
  fileCache: new Map(),
  currentPath: "/",
  selectedIndex: 0,
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

  fetchFiles: async (path, client) => {
    // Check SWR cache
    const cached = get().fileCache.get(path);
    if (cached && Date.now() - cached.fetchedAt < CACHE_TTL_MS) {
      return;
    }

    try {
      const response = await client.get<{ items: readonly FileItem[] }>(
        `/api/v2/files/list?path=${encodeURIComponent(path)}`,
      );

      const items = response.items ?? [];
      const sorted = [...items].sort((a, b) => {
        // Directories first, then alphabetical
        if (a.isDirectory !== b.isDirectory) return a.isDirectory ? -1 : 1;
        return a.name.localeCompare(b.name);
      });

      const newCache = new Map(get().fileCache);
      newCache.set(path, { data: sorted, fetchedAt: Date.now() });
      // Evict oldest entries if cache exceeds 200 paths
      if (newCache.size > 200) {
        const oldest = newCache.keys().next().value;
        if (oldest !== undefined) newCache.delete(oldest);
      }
      set({ fileCache: newCache, error: null });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch files";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchPreview: async (path, client) => {
    set({ previewPath: path, previewLoading: true, previewContent: null });

    try {
      const response = await client.get<{ content: string }>(
        `/api/v2/files/read?path=${encodeURIComponent(path)}`,
      );
      set({ previewContent: response.content ?? "", previewLoading: false });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch preview";
      set({
        previewContent: null,
        previewLoading: false,
        error: message,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  expandNode: async (path, client) => {
    const nodes = get().treeNodes;
    const existing = nodes.get(path);

    if (existing?.expanded) return;

    // Mark as loading
    const loadingNodes = new Map(nodes);
    loadingNodes.set(path, {
      ...(existing ?? { path, name: path.split("/").pop() ?? path, isDirectory: true, children: [], depth: 0, size: 0 }),
      expanded: true,
      loading: true,
    });
    set({ treeNodes: loadingNodes });

    try {
      const response = await client.get<{ items: readonly FileItem[] }>(
        `/api/v2/files/list?path=${encodeURIComponent(path)}`,
      );

      const items = response.items ?? [];
      const sorted = [...items].sort((a, b) => {
        if (a.isDirectory !== b.isDirectory) return a.isDirectory ? -1 : 1;
        return a.name.localeCompare(b.name);
      });

      const parentDepth = existing?.depth ?? 0;
      const updatedNodes = new Map(get().treeNodes);

      // Update parent node
      updatedNodes.set(path, {
        ...updatedNodes.get(path)!,
        expanded: true,
        loading: false,
        children: sorted.map((item) => item.path),
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
          });
        }
      }

      set({ treeNodes: updatedNodes, error: null });
    } catch (err) {
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
    }
  },

  collapseNode: (path) => {
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
    const newCache = new Map(get().fileCache);
    newCache.delete(path);
    set({ fileCache: newCache });
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
