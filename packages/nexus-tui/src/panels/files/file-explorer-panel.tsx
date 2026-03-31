/**
 * Full file explorer layout: left pane (tree) + right pane (preview/metadata).
 *
 * This is the main files panel, loaded lazily by the app.
 *
 * Panel-level tabs: Explorer | Share Links | Uploads
 *
 * Keyboard modes:
 *   - "none"   → normal navigation (j/k, expand/collapse, etc.)
 *   - "mkdir"   → text input for new directory name
 *   - "rename"  → text input for renaming
 *   - "filter"  → fuzzy filter on visible tree (client-side)
 *   - "search"  → power search input (g: glob, r: grep, plain = deep search)
 *   - "visual"  → visual mode for range selection (not an input mode)
 *
 * @see Issue #3101 — filter/search, bulk ops, move/copy
 * @see Issue #3102 — TUI rendering & data-fetching performance
 */

import React, { useState, useCallback, useEffect, useMemo, useRef } from "react";
import {
  useFilesStore,
  type FileItem,
  type TreeNode,
  getEffectiveSelection,
} from "../../stores/files-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useShareLinkStore } from "../../stores/share-link-store.js";
import { useUploadStore } from "../../stores/upload-store.js";
import { Breadcrumb } from "../../shared/components/breadcrumb.js";
import { ConfirmDialog } from "../../shared/components/confirm-dialog.js";
import { FileTree, flattenVisibleNodes, LOAD_MORE_SENTINEL } from "./file-tree.js";
import { FilePreview } from "./file-preview.js";
import { FileEditor } from "./file-editor.js";
import { FileMetadata } from "./file-metadata.js";
import { FileAspects } from "./file-aspects.js";
import { FileSchema } from "./file-schema.js";
import { ShareLinksTab } from "./share-links-tab.js";
import { UploadsTab } from "./uploads-tab.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import {
  listNavigationBindings,
  jumpToEnd,
} from "../../shared/hooks/use-list-navigation.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useBrickAvailable } from "../../shared/hooks/use-brick-available.js";
import { useVisibleTabs, type TabDef } from "../../shared/hooks/use-visible-tabs.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { useUiStore } from "../../stores/ui-store.js";
import { useAnnouncementStore } from "../../stores/announcement-store.js";
import { focusColor, statusColor } from "../../shared/theme.js";
import {
  formatDirectoryAnnouncement,
  formatSelectionAnnouncement,
  formatSuccessAnnouncement,
} from "../../shared/accessibility-announcements.js";
import { textStyle } from "../../shared/text-style.js";
import { formatActionHints, getFilesFooterBindings } from "../../shared/action-registry.js";
import crypto from "node:crypto";

// =============================================================================
// Panel-level tabs
// =============================================================================

type FilesTab = "explorer" | "shareLinks" | "uploads";

const ALL_TABS: readonly TabDef<FilesTab>[] = [
  { id: "explorer", label: "Explorer", brick: null },
  { id: "shareLinks", label: "Share Links", brick: "share_link" },
  { id: "uploads", label: "Uploads", brick: "uploads" },
];

// =============================================================================
// Input mode types
// =============================================================================

type InputMode = "none" | "mkdir" | "rename" | "filter" | "search" | "paste-dest" | "create";

// =============================================================================
// Keybinding builders — one function per mode (Decision 6A)
// =============================================================================

interface BindingContext {
  // Active tab
  readonly activeTab: FilesTab;
  // Explorer
  readonly cachedFiles: readonly FileItem[];
  readonly selectedIndex: number;
  readonly selectedItem: FileItem | null;
  readonly selectedNode: TreeNode | null;
  readonly isSentinel: boolean;
  readonly visibleNodeCount: number;
  readonly currentPath: string;
  readonly client: ReturnType<typeof useApi>;
  // Stores
  readonly setSelectedIndex: (i: number) => void;
  readonly toggleNode: (path: string, client: NonNullable<ReturnType<typeof useApi>>) => Promise<void>;
  readonly collapseNode: (path: string) => void;
  readonly fetchPreview: (path: string, client: NonNullable<ReturnType<typeof useApi>>) => Promise<void>;
  readonly setMetadataTab: (tab: "metadata" | "aspects" | "schema") => void;
  readonly catalogAvailable: boolean;
  // Share links
  readonly shareLinks: readonly { link_id: string; status: string }[];
  readonly selectedLinkIndex: number;
  readonly setSelectedLinkIndex: (i: number) => void;
  readonly revokeLink: (id: string, client: NonNullable<ReturnType<typeof useApi>>) => void;
  readonly fetchLinks: (client: NonNullable<ReturnType<typeof useApi>>) => void;
  // Uploads
  readonly uploadSessions: readonly unknown[];
  readonly selectedSessionIndex: number;
  readonly setSelectedSessionIndex: (i: number) => void;
  // Tabs
  readonly visibleTabs: readonly TabDef<FilesTab>[];
  readonly setActiveTab: (tab: FilesTab) => void;
  readonly toggleFocus: (panel: string) => void;
  // Actions
  readonly copy: (text: string) => void;
  readonly setConfirmDelete: (v: boolean) => void;
  readonly setInputMode: (mode: InputMode) => void;
  readonly setInputBuffer: (v: string | ((prev: string) => string)) => void;
  // Selection & clipboard
  readonly selectedPaths: ReadonlySet<string>;
  readonly visualModeAnchor: number | null;
  readonly clipboard: { readonly paths: readonly string[]; readonly operation: "copy" | "cut" } | null;
  readonly toggleSelect: (path: string) => void;
  readonly clearSelection: () => void;
  readonly enterVisualMode: (anchor: number) => void;
  readonly exitVisualMode: () => void;
  readonly yankToClipboard: (paths: readonly string[]) => void;
  readonly cutToClipboard: (paths: readonly string[]) => void;
  readonly clearClipboard: () => void;
  readonly pasteFiles: (destinationDir: string, client: NonNullable<ReturnType<typeof useApi>>) => Promise<void>;
  // Filter
  readonly filterQuery: string;
  readonly setFilterQuery: (v: string | ((prev: string) => string)) => void;
  readonly searchQuery: string;
  readonly setSearchQuery: (v: string | ((prev: string) => string)) => void;
  readonly executeSearch: (query: string) => void;
  // Search results
  readonly searchResults: readonly { path: string; line?: number; content?: string }[] | null;
  readonly setSearchResults: (v: readonly { path: string; line?: number; content?: string }[] | null) => void;
  // Paste destination input
  readonly setInputModeWithCallback: (mode: InputMode, onSubmit: (value: string) => void) => void;
  // Editor
  readonly openEditor: (path: string) => void;
}

/** Navigation bindings for the currently active tab (Decision 5A). */
function getTabNavBindings(ctx: BindingContext): Record<string, () => void> {
  switch (ctx.activeTab) {
    case "explorer":
      return {
        ...listNavigationBindings({
          getIndex: () => ctx.selectedIndex,
          setIndex: ctx.setSelectedIndex,
          getLength: () => ctx.visibleNodeCount,
          onSelect: (index) => {
            // Sentinel nodes are handled by FileTree's auto-load effect
            if (ctx.isSentinel) return;
            if (ctx.selectedNode && ctx.client) {
              if (ctx.selectedNode.isDirectory) {
                ctx.toggleNode(ctx.selectedNode.path, ctx.client);
              } else {
                ctx.fetchPreview(ctx.selectedNode.path, ctx.client);
              }
            }
          },
        }),
        // g = jump to start (not in listNavigationBindings)
        g: () => ctx.setSelectedIndex(0),
      };
    case "shareLinks":
      return {
        ...listNavigationBindings({
          getIndex: () => ctx.selectedLinkIndex,
          setIndex: ctx.setSelectedLinkIndex,
          getLength: () => ctx.shareLinks.length,
        }),
        g: () => ctx.setSelectedLinkIndex(0),
      };
    case "uploads":
      return {
        ...listNavigationBindings({
          getIndex: () => ctx.selectedSessionIndex,
          setIndex: ctx.setSelectedSessionIndex,
          getLength: () => ctx.uploadSessions.length,
        }),
        g: () => ctx.setSelectedSessionIndex(0),
      };
  }
}

/** Tab cycling (shared across all modes). */
function getTabCycleBindings(ctx: BindingContext): Record<string, () => void> {
  return {
    tab: () => {
      const ids = ctx.visibleTabs.map((t) => t.id);
      const idx = ids.indexOf(ctx.activeTab);
      const next = ids[(idx + 1) % ids.length];
      if (next) ctx.setActiveTab(next);
    },
    "shift+tab": () => ctx.toggleFocus("files"),
  };
}

/** Explorer-specific action bindings (not navigation). */
function getExplorerActionBindings(ctx: BindingContext): Record<string, () => void> {
  return {
    // Tree navigation
    l: () => {
      if (ctx.isSentinel) return;
      if (ctx.selectedNode?.isDirectory && ctx.client) ctx.toggleNode(ctx.selectedNode.path, ctx.client);
    },
    h: () => {
      if (ctx.isSentinel) return;
      if (ctx.selectedNode?.isDirectory) ctx.collapseNode(ctx.selectedNode.path);
    },
    // Metadata tabs
    m: () => ctx.setMetadataTab("metadata"),
    ...(ctx.catalogAvailable ? {
      a: () => ctx.setMetadataTab("aspects"),
      s: () => ctx.setMetadataTab("schema"),
    } : {}),
    // File operations — bulk delete if selection active, otherwise single item
    d: () => {
      if (ctx.isSentinel) return;
      const effective = getEffectiveSelection(
        ctx.selectedPaths, ctx.visualModeAnchor, ctx.selectedIndex,
        ctx.cachedFiles.map((f) => f.path),
      );
      if (effective.size > 0 || ctx.selectedItem) {
        ctx.setConfirmDelete(true);
      }
    },
    "shift+n": () => { ctx.setInputMode("mkdir"); ctx.setInputBuffer(""); },
    "shift+r": () => {
      if (ctx.isSentinel) return;
      if (ctx.selectedItem) {
        ctx.setInputMode("rename");
        ctx.setInputBuffer(ctx.selectedItem.name);
      }
    },
    // Edit existing file: open full-screen editor
    e: () => {
      if (ctx.selectedItem && !ctx.selectedItem.isDirectory) {
        ctx.openEditor(ctx.selectedItem.path);
      }
    },
    // Create new file: prompt for filename, then open editor
    "shift+e": () => {
      const dir = ctx.selectedItem?.isDirectory
        ? ctx.selectedItem.path
        : ctx.currentPath;
      const prefix = dir === "/" ? "/" : dir + "/";
      ctx.setInputMode("create");
      ctx.setInputBuffer(prefix);
    },
    // Copy path to system clipboard
    y: () => {
      if (ctx.isSentinel) return;
      if (ctx.selectedItem) ctx.copy(ctx.selectedItem.path);
    },
    // Selection
    space: () => {
      if (ctx.isSentinel) return;
      const item = ctx.cachedFiles[ctx.selectedIndex];
      if (item) ctx.toggleSelect(item.path);
    },
    // Visual mode
    v: () => {
      if (ctx.visualModeAnchor !== null) {
        ctx.exitVisualMode();
      } else {
        ctx.enterVisualMode(ctx.selectedIndex);
      }
    },
    // Clipboard: copy/cut/paste
    c: () => {
      const effective = getEffectiveSelection(
        ctx.selectedPaths, ctx.visualModeAnchor, ctx.selectedIndex,
        ctx.cachedFiles.map((f) => f.path),
      );
      if (effective.size > 0) {
        ctx.yankToClipboard([...effective]);
        ctx.clearSelection();
      }
    },
    x: () => {
      const effective = getEffectiveSelection(
        ctx.selectedPaths, ctx.visualModeAnchor, ctx.selectedIndex,
        ctx.cachedFiles.map((f) => f.path),
      );
      if (effective.size > 0) {
        ctx.cutToClipboard([...effective]);
        ctx.clearSelection();
      }
    },
    p: () => {
      if (ctx.clipboard && ctx.client) {
        ctx.pasteFiles(ctx.currentPath, ctx.client);
      }
    },
    // Shift+P: paste to a specific path (prompts for destination)
    "shift+p": () => {
      if (ctx.clipboard && ctx.client) {
        ctx.setInputMode("paste-dest");
        ctx.setInputBuffer(ctx.currentPath);
      }
    },
    // Escape: dismiss search results > exit visual mode > clear selection
    escape: () => {
      if (ctx.searchResults !== null) {
        ctx.setSearchResults(null);
      } else if (ctx.visualModeAnchor !== null) {
        ctx.exitVisualMode();
      } else if (ctx.selectedPaths.size > 0) {
        ctx.clearSelection();
      }
    },
    // Filter & search modes
    "/": () => { ctx.setInputMode("filter"); ctx.setInputBuffer(""); },
    "ctrl+f": () => { ctx.setInputMode("search"); ctx.setInputBuffer(""); },
  };
}

/** ShareLinks-specific action bindings. */
function getShareLinksActionBindings(ctx: BindingContext): Record<string, () => void> {
  return {
    x: () => {
      if (ctx.client) {
        const link = ctx.shareLinks[ctx.selectedLinkIndex] as { link_id: string; status: string } | undefined;
        if (link && link.status === "active") {
          ctx.revokeLink(link.link_id, ctx.client);
        }
      }
    },
    r: () => { if (ctx.client) ctx.fetchLinks(ctx.client); },
  };
}

/** Input mode bindings (mkdir, rename, filter, search). */
function getInputModeBindings(
  inputMode: InputMode,
  ctx: BindingContext,
): Record<string, () => void> {
  const resetInput = () => {
    ctx.setInputMode("none");
    ctx.setInputBuffer("");
  };

  const baseBindings: Record<string, () => void> = {
    escape: resetInput,
    backspace: () => ctx.setInputBuffer((b) => b.slice(0, -1)),
  };

  switch (inputMode) {
    case "mkdir":
      return {
        ...baseBindings,
        return: () => {
          const value = ctx.filterQuery.trim(); // inputBuffer is used but accessed via closure
          if (!value || !ctx.client) { resetInput(); return; }
          const dirPath = ctx.currentPath === "/" ? `/${value}` : `${ctx.currentPath}/${value}`;
          useFilesStore.getState().mkdirFile(dirPath, ctx.client);
          resetInput();
        },
      };

    case "rename":
      return {
        ...baseBindings,
        return: () => {
          const value = ctx.filterQuery.trim();
          if (!value || !ctx.client || !ctx.selectedItem) { resetInput(); return; }
          const parentPath = ctx.selectedItem.path.split("/").slice(0, -1).join("/") || "/";
          const newPath = parentPath === "/" ? `/${value}` : `${parentPath}/${value}`;
          useFilesStore.getState().renameFile(ctx.selectedItem.path, newPath, ctx.client);
          resetInput();
        },
      };

    case "filter":
      return {
        ...baseBindings,
        return: resetInput, // confirm filter and return to normal mode (filter stays applied)
        escape: () => {
          ctx.setFilterQuery("");
          resetInput();
        },
      };

    case "search":
      return {
        ...baseBindings,
        return: () => {
          const query = ctx.searchQuery.trim();
          if (query) ctx.executeSearch(query);
          resetInput();
        },
        escape: () => {
          ctx.setSearchQuery("");
          resetInput();
        },
      };

    case "paste-dest":
      return {
        ...baseBindings,
        return: () => {
          const dest = ctx.filterQuery.trim();
          if (dest && ctx.client) {
            useFilesStore.getState().pasteFiles(dest, ctx.client);
          }
          resetInput();
        },
      };

    case "create":
      return {
        ...baseBindings,
        return: () => {
          const filePath = ctx.filterQuery.trim();
          if (!filePath) { resetInput(); return; }
          // Open editor for the new path (editor handles creation on save)
          ctx.openEditor(filePath);
          resetInput();
        },
      };

    default:
      return {};
  }
}

/** Top-level binding dispatch based on current mode. */
function getKeyBindings(
  inputMode: InputMode,
  overlayActive: boolean,
  confirmDelete: boolean,
  editorOpen: boolean,
  ctx: BindingContext,
): Record<string, () => void> {
  if (overlayActive || confirmDelete || editorOpen) return {};

  if (inputMode !== "none") {
    return getInputModeBindings(inputMode, ctx);
  }

  // Normal mode: navigation + tab cycling + tab-specific actions
  const navBindings = getTabNavBindings(ctx);
  const tabBindings = getTabCycleBindings(ctx);

  const actionBindings = ctx.activeTab === "explorer"
    ? getExplorerActionBindings(ctx)
    : ctx.activeTab === "shareLinks"
      ? getShareLinksActionBindings(ctx)
      : {};

  return { ...navBindings, ...tabBindings, ...actionBindings };
}

// =============================================================================
// Input bar label per mode
// =============================================================================

function getInputLabel(mode: InputMode, buffer: string): string {
  switch (mode) {
    case "mkdir": return `New directory: ${buffer}\u2588`;
    case "rename": return `Rename to: ${buffer}\u2588`;
    case "filter": return `/${buffer}\u2588`;
    case "search": return `Search (g: glob, r: grep): ${buffer}\u2588`;
    case "paste-dest": return `Paste to: ${buffer}\u2588`;
    case "create": return `New file path: ${buffer}\u2588`;
    default: return "";
  }
}

// =============================================================================
// Component
// =============================================================================

export default function FileExplorerPanel(): React.ReactNode {
  const client = useApi();
  const visibleTabs = useVisibleTabs(ALL_TABS);

  // Panel-level active tab
  const [activeTab, setActiveTab] = useState<FilesTab>("explorer");

  // Fall back to first visible tab if the active tab becomes hidden
  const visibleIds = visibleTabs.map((t) => t.id);
  useEffect(() => {
    if (visibleIds.length > 0 && !visibleIds.includes(activeTab)) {
      setActiveTab(visibleIds[0]!);
    }
  }, [visibleIds.join(","), activeTab]);

  // Files store
  const currentPath = useFilesStore((s) => s.currentPath);
  const setCurrentPath = useFilesStore((s) => s.setCurrentPath);
  const treeNodes = useFilesStore((s) => s.treeNodes);
  const fileCacheRevision = useFilesStore((s) => s.fileCacheRevision);
  const getCachedFiles = useFilesStore((s) => s.getCachedFiles);
  const abortAll = useFilesStore((s) => s.abortAllInFlight);
  const selectedIndex = useFilesStore((s) => s.selectedIndex);
  const toggleNode = useFilesStore((s) => s.toggleNode);
  const collapseNode = useFilesStore((s) => s.collapseNode);
  const setSelectedIndex = useFilesStore((s) => s.setSelectedIndex);
  const fetchPreview = useFilesStore((s) => s.fetchPreview);

  // Cancel all in-flight file requests when panel unmounts (Issue #3102)
  useEffect(() => {
    return () => { abortAll(); };
  }, [abortAll]);

  // Selection & clipboard store
  const selectedPaths = useFilesStore((s) => s.selectedPaths);
  const visualModeAnchor = useFilesStore((s) => s.visualModeAnchor);
  const clipboard = useFilesStore((s) => s.clipboard);
  const toggleSelect = useFilesStore((s) => s.toggleSelect);
  const clearSelection = useFilesStore((s) => s.clearSelection);
  const enterVisualMode = useFilesStore((s) => s.enterVisualMode);
  const exitVisualMode = useFilesStore((s) => s.exitVisualMode);
  const yankToClipboard = useFilesStore((s) => s.yankToClipboard);
  const cutToClipboard = useFilesStore((s) => s.cutToClipboard);
  const clearClipboard = useFilesStore((s) => s.clearClipboard);
  const pasteFiles = useFilesStore((s) => s.pasteFiles);
  const pasteProgress = useFilesStore((s) => s.pasteProgress);
  const announce = useAnnouncementStore((s) => s.announce);

  // Share link store
  const shareLinks = useShareLinkStore((s) => s.links);
  const shareLinksLoading = useShareLinkStore((s) => s.linksLoading);
  const selectedLinkIndex = useShareLinkStore((s) => s.selectedLinkIndex);
  const fetchLinks = useShareLinkStore((s) => s.fetchLinks);
  const setSelectedLinkIndex = useShareLinkStore((s) => s.setSelectedLinkIndex);

  // Upload store
  const uploadSessions = useUploadStore((s) => s.sessions);
  const selectedSessionIndex = useUploadStore((s) => s.selectedSessionIndex);
  const setSelectedSessionIndex = useUploadStore((s) => s.setSelectedSessionIndex);
  const revokeLink = useShareLinkStore((s) => s.revokeLink);

  // UI store
  const uiFocusPane = useUiStore((s) => s.getFocusPane("files"));
  const toggleFocus = useUiStore((s) => s.toggleFocusPane);
  const overlayActive = useUiStore((s) => s.overlayActive);
  const setOverlayActive = useUiStore((s) => s.setOverlayActive);

  // Catalog brick availability
  const { available: catalogAvailable } = useBrickAvailable("catalog");

  // Active metadata sub-tab
  const [metadataTab, setMetadataTab] = React.useState<"metadata" | "aspects" | "schema">("metadata");
  React.useEffect(() => {
    if (!catalogAvailable && (metadataTab === "aspects" || metadataTab === "schema")) {
      setMetadataTab("metadata");
    }
  }, [catalogAvailable, metadataTab]);

  // Flattened visible tree nodes — the source of truth for explorer navigation.
  const visibleNodes = useMemo(
    () => flattenVisibleNodes(currentPath, treeNodes),
    [currentPath, treeNodes],
  );

  const selectedNode = visibleNodes[selectedIndex] ?? null;
  const isSentinel = selectedNode?.path.endsWith(LOAD_MORE_SENTINEL) ?? false;
  const currentTreeNode = treeNodes.get(currentPath);
  const lastDirectoryAnnouncementRef = useRef<string | null>(null);
  const lastSelectionAnnouncementRef = useRef<string | null>(null);
  const lastPasteAnnouncementRef = useRef<string | null>(null);

  // For metadata/actions, look up FileItem from parent's file cache first,
  // then fall back to constructing a minimal FileItem from the tree node.
  // The fallback ensures metadata pane works even if the file cache is empty
  // (e.g. requests were aborted during rapid navigation).
  const selectedItem: FileItem | null = useMemo(() => {
    if (!selectedNode || isSentinel) return null;
    const parentDir = selectedNode.path.split("/").slice(0, -1).join("/") || "/";
    const parentFiles = getCachedFiles(parentDir);
    const cached = parentFiles?.find((f) => f.path === selectedNode.path);
    if (cached) return cached;
    // Fallback: construct from tree node, using global zoneId from health check
    return {
      name: selectedNode.name,
      path: selectedNode.path,
      isDirectory: selectedNode.isDirectory,
      size: selectedNode.size ?? 0,
      modifiedAt: null,
      etag: null,
      mimeType: null,
      version: null,
      owner: null,
      permissions: null,
      zoneId: useGlobalStore.getState().zoneId,
    };
  }, [selectedNode, isSentinel, getCachedFiles, fileCacheRevision]);

  const visibleNodeCount = visibleNodes.length;
  // Keep cachedFiles for backward compat with BindingContext (selection uses it)
  const cachedFiles = fileCacheRevision >= 0 ? (getCachedFiles(currentPath) ?? []) : [];

  useEffect(() => {
    lastSelectionAnnouncementRef.current = null;
  }, [currentPath]);

  useEffect(() => {
    if (pasteProgress === null) {
      lastPasteAnnouncementRef.current = null;
    }
  }, [pasteProgress]);

  useEffect(() => {
    if (!currentTreeNode || currentTreeNode.loading) return;
    const key = `${currentPath}:${cachedFiles.length}:${fileCacheRevision}`;
    if (lastDirectoryAnnouncementRef.current === key) return;
    lastDirectoryAnnouncementRef.current = key;
    announce(formatDirectoryAnnouncement(currentPath, cachedFiles.length));
  }, [currentTreeNode, currentPath, cachedFiles.length, fileCacheRevision, announce]);

  useEffect(() => {
    if (!selectedNode || isSentinel) return;
    if (lastSelectionAnnouncementRef.current === null) {
      lastSelectionAnnouncementRef.current = selectedNode.path;
      return;
    }
    if (lastSelectionAnnouncementRef.current === selectedNode.path) return;
    lastSelectionAnnouncementRef.current = selectedNode.path;
    announce(formatSelectionAnnouncement(selectedNode.name, selectedNode.isDirectory));
  }, [selectedNode, isSentinel, announce]);

  useEffect(() => {
    if (!pasteProgress) return;
    const completed = pasteProgress.completed + pasteProgress.failed;
    if (completed < pasteProgress.total) return;
    const key = `${pasteProgress.total}:${pasteProgress.completed}:${pasteProgress.failed}:${clipboard?.operation ?? "none"}`;
    if (lastPasteAnnouncementRef.current === key) return;
    lastPasteAnnouncementRef.current = key;
    announce(
      formatSuccessAnnouncement(
        pasteProgress.failed > 0
          ? `Paste complete: ${pasteProgress.completed} succeeded, ${pasteProgress.failed} failed`
          : `Paste complete: ${pasteProgress.completed} items`,
      ),
      pasteProgress.failed > 0 ? "error" : "success",
    );
  }, [pasteProgress, clipboard?.operation, announce]);

  // Aspect count badge
  const aspectsCache = useKnowledgeStore((s) => s.aspectsCache);
  const selectedUrn = selectedItem?.path
    ? `urn:nexus:file:${selectedItem.zoneId || "default"}:${crypto.createHash("sha256").update(selectedItem.path).digest("hex").slice(0, 32)}`
    : null;
  const aspectCount = selectedUrn ? (aspectsCache.get(selectedUrn)?.length ?? 0) : 0;

  // Clipboard copy (system)
  const { copy, copied } = useCopy();

  // Editor overlay state — suppress global panel-switch keys while editor is open
  const [editorPath, setEditorPath] = useState<string | null>(null);
  const openEditor = useCallback((path: string) => {
    useUiStore.getState().setFileEditorOpen(true);
    setEditorPath(path);
  }, []);
  const closeEditor = useCallback(() => {
    useUiStore.getState().setFileEditorOpen(false);
    setEditorPath(null);
  }, []);

  // Dialog state
  const [confirmDelete, setConfirmDelete] = useState(false);

  // Input mode
  const [inputMode, setInputMode] = useState<InputMode>("none");
  const [inputBuffer, setInputBuffer] = useState("");

  // Filter & search state
  const [filterQuery, setFilterQuery] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<readonly { path: string; line?: number; content?: string }[] | null>(null);

  // Effective selection count for display
  const effectiveSelection = useMemo(() => {
    if (activeTab !== "explorer") return new Set<string>();
    return getEffectiveSelection(
      selectedPaths, visualModeAnchor, selectedIndex,
      cachedFiles.map((f) => f.path),
    );
  }, [activeTab, selectedPaths, visualModeAnchor, selectedIndex, cachedFiles]);

  // Fetch share links when switching to that tab
  useEffect(() => {
    if (!client) return;
    if (activeTab === "shareLinks") fetchLinks(client);
  }, [activeTab, client, fetchLinks]);

  // Search execution
  const executeSearch = useCallback(async (query: string) => {
    if (!client) return;
    setSearchResults(null);

    try {
      if (query.startsWith("g:")) {
        // Glob search
        const pattern = query.slice(2).trim();
        if (!pattern) return;
        const res = await client.get<{ matches: string[]; total: number; truncated: boolean }>(
          `/api/v2/files/glob?pattern=${encodeURIComponent(pattern)}&path=${encodeURIComponent(currentPath)}&limit=100`,
        );
        setSearchResults(res.matches.map((p: string) => ({ path: p })));
      } else if (query.startsWith("r:")) {
        // Grep search
        const pattern = query.slice(2).trim();
        if (!pattern) return;
        const res = await client.get<{ matches: { file: string; line: number; content: string }[]; total: number; truncated: boolean }>(
          `/api/v2/files/grep?pattern=${encodeURIComponent(pattern)}&path=${encodeURIComponent(currentPath)}&limit=100`,
        );
        setSearchResults(res.matches.map((m: { file: string; line: number; content: string }) => ({ path: m.file, line: m.line, content: m.content })));
      } else {
        // Deep search via search API
        const res = await client.get<{ results: { path: string }[] }>(
          `/api/v2/search/query?q=${encodeURIComponent(query)}&path=${encodeURIComponent(currentPath)}&limit=100`,
        );
        setSearchResults(res.results.map((r: { path: string }) => ({ path: r.path })));
      }
    } catch {
      setSearchResults([]);
    }
  }, [client, currentPath]);

  // Build input buffer reference for the binding context
  // The input buffer needs to be passed through the context for mkdir/rename
  // to access the current value in their return handlers
  const inputBufferRef = inputMode === "filter" ? filterQuery
    : inputMode === "search" ? searchQuery
    : inputMode === "paste-dest" ? inputBuffer
    : inputBuffer;

  // Handle unhandled keys for text input modes
  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      if (inputMode === "none") return;
      const setter = inputMode === "filter" ? setFilterQuery
        : inputMode === "search" ? setSearchQuery
        : setInputBuffer;
      if (keyName.length === 1) {
        setter((b) => b + keyName);
      } else if (keyName === "space") {
        setter((b) => b + " ");
      }
    },
    [inputMode],
  );

  // Build binding context
  const ctx: BindingContext = {
    activeTab, cachedFiles, selectedIndex, selectedItem, selectedNode, isSentinel,
    visibleNodeCount, currentPath, client, setSelectedIndex, toggleNode, collapseNode,
    fetchPreview, setMetadataTab, catalogAvailable,
    shareLinks, selectedLinkIndex, setSelectedLinkIndex, revokeLink, fetchLinks,
    uploadSessions, selectedSessionIndex, setSelectedSessionIndex,
    visibleTabs, setActiveTab, toggleFocus, copy, setConfirmDelete,
    setInputMode, setInputBuffer,
    selectedPaths, visualModeAnchor, clipboard,
    toggleSelect, clearSelection, enterVisualMode, exitVisualMode,
    yankToClipboard, cutToClipboard, clearClipboard, pasteFiles,
    filterQuery: inputBufferRef, setFilterQuery, searchQuery, setSearchQuery,
    executeSearch,
    searchResults, setSearchResults,
    setInputModeWithCallback: setInputMode as BindingContext["setInputModeWithCallback"],
    openEditor,
  };

  useKeyboard(
    getKeyBindings(inputMode, overlayActive, confirmDelete, editorPath !== null, ctx),
    !overlayActive && inputMode !== "none" && editorPath === null ? handleUnhandledKey : undefined,
  );

  const handleConfirmDelete = (): void => {
    setConfirmDelete(false);
    if (!client) return;
    // Bulk delete: delete all selected files, then fall back to single item
    const effective = getEffectiveSelection(
      selectedPaths, visualModeAnchor, selectedIndex,
      cachedFiles.map((f) => f.path),
    );
    if (effective.size > 0) {
      for (const path of effective) {
        useFilesStore.getState().deleteFile(path, client);
      }
      clearSelection();
    } else if (selectedItem) {
      useFilesStore.getState().deleteFile(selectedItem.path, client);
    }
  };

  const handleCancelDelete = (): void => {
    setConfirmDelete(false);
  };

  // Determine which input buffer to display
  const displayBuffer = inputMode === "filter" ? filterQuery
    : inputMode === "search" ? searchQuery
    : inputBuffer;

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Full-screen file editor */}
      {editorPath ? (
        <FileEditor path={editorPath} onClose={closeEditor} />
      ) : <>

      {/* Panel-level tab bar */}
      <box height={1} width="100%">
        <text>
          {visibleTabs.map((tab) => {
            return tab.id === activeTab ? `[${tab.label}]` : ` ${tab.label} `;
          }).join(" ")}
        </text>
      </box>

      {/* Input bar for text modes */}
      {inputMode !== "none" && (
        <box height={1} width="100%">
          <text>{getInputLabel(inputMode, displayBuffer)}</text>
        </box>
      )}

      {/* Paste progress indicator */}
      {pasteProgress && (
        <box height={1} width="100%">
          <text style={textStyle({ fg: "cyan" })}>
            {pasteProgress.completed + pasteProgress.failed >= pasteProgress.total
              ? `Paste complete: ${pasteProgress.completed}/${pasteProgress.total}${pasteProgress.failed > 0 ? ` (${pasteProgress.failed} failed)` : ""}`
              : `Pasting... ${pasteProgress.completed + pasteProgress.failed}/${pasteProgress.total}${pasteProgress.failed > 0 ? ` (${pasteProgress.failed} failed)` : ""}`}
          </text>
        </box>
      )}

      {/* Clipboard indicator (only when not actively pasting) */}
      {clipboard && !pasteProgress && inputMode === "none" && (
        <box height={1} width="100%">
          <text style={textStyle({ fg: "yellow" })}>
            {`${clipboard.paths.length} file${clipboard.paths.length > 1 ? "s" : ""} ${clipboard.operation === "cut" ? "cut" : "copied"} — press p to paste`}
          </text>
        </box>
      )}

      {/* Explorer tab */}
      {activeTab === "explorer" && (
        <box flexGrow={1} flexDirection="column">
          {/* Breadcrumb navigation */}
          <Breadcrumb path={currentPath} onNavigate={setCurrentPath} />

          {/* Search results overlay */}
          {searchResults !== null ? (
            <box flexGrow={1} borderStyle="single">
              <scrollbox height="100%" width="100%">
                {searchResults.length === 0
                  ? <text>No results found</text>
                  : searchResults.map((result, i) => (
                    <box key={`${result.path}:${result.line ?? i}`} height={1} width="100%">
                      <text>
                        {result.line !== undefined
                          ? `${result.path}:${result.line}  ${result.content ?? ""}`
                          : result.path}
                      </text>
                    </box>
                  ))}
                <box height={1}>
                  <text style={textStyle({ dim: true })}>Press Escape to return to explorer</text>
                </box>
              </scrollbox>
            </box>
          ) : (
            /* Main content: tree + preview */
            <box flexGrow={1} flexDirection="row">
              {/* Left pane: file tree (40%) */}
              <box width="40%" height="100%" borderStyle="single" borderColor={uiFocusPane === "left" ? focusColor.activeBorder : focusColor.inactiveBorder}>
                <FileTree
                  filterQuery={filterQuery}
                  effectiveSelection={effectiveSelection}
                />
              </box>

              {/* Right pane: preview + metadata (60%) */}
              <box width="60%" height="100%" flexDirection="column" borderStyle="single" borderColor={uiFocusPane === "right" ? focusColor.activeBorder : focusColor.inactiveBorder}>
                {/* File preview (top 70%) */}
                <box flexGrow={7} borderStyle="single">
                  <FilePreview />
                </box>

                {/* Metadata tab bar with aspect count badge */}
                <box height={1} width="100%">
                  <text>
                    {`  ${metadataTab === "metadata" ? "[Metadata]" : " Metadata "}${catalogAvailable ? ` ${metadataTab === "aspects" ? `[Aspects${aspectCount > 0 ? ` (${aspectCount})` : ""}]` : ` Aspects${aspectCount > 0 ? ` (${aspectCount})` : ""} `} ${metadataTab === "schema" ? "[Schema]" : " Schema "}` : ""}`}
                  </text>
                </box>

                {/* Metadata sidebar (bottom 30%) */}
                <box flexGrow={3} borderStyle="single">
                  {metadataTab === "metadata" && <FileMetadata item={selectedItem} />}
                  {metadataTab === "aspects" && catalogAvailable && <FileAspects item={selectedItem} />}
                  {metadataTab === "schema" && catalogAvailable && <FileSchema item={selectedItem} />}
                </box>
              </box>
            </box>
          )}
        </box>
      )}

      {/* Share Links tab */}
      {activeTab === "shareLinks" && (
        <box flexGrow={1} borderStyle="single">
          <ShareLinksTab
            links={shareLinks}
            selectedIndex={selectedLinkIndex}
            loading={shareLinksLoading}
          />
        </box>
      )}

      {/* Uploads tab */}
      {activeTab === "uploads" && (
        <box flexGrow={1} borderStyle="single">
          <UploadsTab
            sessions={uploadSessions}
            selectedIndex={selectedSessionIndex}
            loading={false}
          />
        </box>
      )}

      {/* Help bar */}
      <box height={1} width="100%">
        {copied
          ? <text style={textStyle({ fg: "green" })}>Copied!</text>
          : <text>
            {formatActionHints(getFilesFooterBindings({
              inputMode,
              activeTab,
              catalogAvailable,
              visualMode: visualModeAnchor !== null,
              selectionCount: effectiveSelection.size,
              clipboard,
            }))}
          </text>}
      </box>

      {/* Delete confirmation dialog */}
      <ConfirmDialog
        visible={confirmDelete}
        title={effectiveSelection.size > 0 ? "Delete Selected" : "Delete File"}
        message={effectiveSelection.size > 1
          ? `Delete ${effectiveSelection.size} selected files?`
          : effectiveSelection.size === 1
            ? `Delete "${[...effectiveSelection][0]!.split("/").pop()}"?`
            : `Delete "${selectedItem?.name ?? ""}"?`}
        onConfirm={handleConfirmDelete}
        onCancel={handleCancelDelete}
      />
    </>}
    </box>
  );
}
