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
}

const SOURCE = "files";

export const useFilesStore = create<FilesState>((set, get) => ({
  fileCache: new Map(),
  currentPath: "/",
  selectedIndex: 0,
  treeNodes: new Map(),
  focusPane: "tree",
  previewPath: null,
  previewContent: null,
  previewLoading: false,
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
      ...(existing ?? { path, name: path.split("/").pop() ?? path, isDirectory: true, children: [], depth: 0 }),
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
    // Note: rename is write+delete — not atomic. If delete fails, file exists at both paths.
    set({ error: null });
    try {
      await client.post("/api/v2/files/write", { path: newPath, source_path: oldPath });
      await client.delete(`/api/v2/files/delete?path=${encodeURIComponent(oldPath)}`);
      const parentPath = oldPath.split("/").slice(0, -1).join("/") || "/";
      get().invalidate(parentPath);
      await get().fetchFiles(parentPath, client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to rename file";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },
}));
