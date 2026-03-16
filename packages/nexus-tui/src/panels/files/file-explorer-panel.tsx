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
 */

import React, { useState, useCallback, useEffect, useMemo } from "react";
import {
  useFilesStore,
  type FileItem,
  getEffectiveSelection,
} from "../../stores/files-store.js";
import { useShareLinkStore } from "../../stores/share-link-store.js";
import { useUploadStore } from "../../stores/upload-store.js";
import { Breadcrumb } from "../../shared/components/breadcrumb.js";
import { ConfirmDialog } from "../../shared/components/confirm-dialog.js";
import { FileTree } from "./file-tree.js";
import { FilePreview } from "./file-preview.js";
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
import { focusColor } from "../../shared/theme.js";
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

type InputMode = "none" | "mkdir" | "rename" | "filter" | "search";

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
            const item = ctx.cachedFiles[index];
            if (item && ctx.client) {
              if (item.isDirectory) {
                ctx.toggleNode(item.path, ctx.client);
              } else {
                ctx.fetchPreview(item.path, ctx.client);
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
      const item = ctx.cachedFiles[ctx.selectedIndex];
      if (item?.isDirectory && ctx.client) ctx.toggleNode(item.path, ctx.client);
    },
    h: () => {
      const item = ctx.cachedFiles[ctx.selectedIndex];
      if (item?.isDirectory) ctx.collapseNode(item.path);
    },
    // Metadata tabs
    m: () => ctx.setMetadataTab("metadata"),
    ...(ctx.catalogAvailable ? {
      a: () => ctx.setMetadataTab("aspects"),
      s: () => ctx.setMetadataTab("schema"),
    } : {}),
    // File operations
    d: () => { if (ctx.selectedItem) ctx.setConfirmDelete(true); },
    N: () => { ctx.setInputMode("mkdir"); ctx.setInputBuffer(""); },
    R: () => {
      if (ctx.selectedItem) {
        ctx.setInputMode("rename");
        ctx.setInputBuffer(ctx.selectedItem.name);
      }
    },
    // Copy path to system clipboard
    y: () => { if (ctx.selectedItem) ctx.copy(ctx.selectedItem.path); },
    // Selection
    space: () => {
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
    // Escape clears selection or exits visual mode
    escape: () => {
      if (ctx.visualModeAnchor !== null) {
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

    default:
      return {};
  }
}

/** Top-level binding dispatch based on current mode. */
function getKeyBindings(
  inputMode: InputMode,
  overlayActive: boolean,
  confirmDelete: boolean,
  ctx: BindingContext,
): Record<string, () => void> {
  if (overlayActive || confirmDelete) return {};

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
    default: return "";
  }
}

// =============================================================================
// Help bar text
// =============================================================================

function getHelpText(
  inputMode: InputMode,
  activeTab: FilesTab,
  catalogAvailable: boolean,
  visualMode: boolean,
  selectionCount: number,
  clipboard: BindingContext["clipboard"],
): string {
  if (inputMode === "filter") return "Type to filter, Enter:keep filter, Escape:clear";
  if (inputMode === "search") return "g:pattern=glob  r:pattern=grep  plain=deep search  Enter:search  Esc:cancel";
  if (inputMode !== "none") return "Type name, Enter:confirm, Escape:cancel, Backspace:delete";

  if (activeTab === "explorer") {
    const parts = ["j/k:nav", "l/Enter:expand", "h:collapse"];
    if (visualMode) {
      parts.push("v:exit visual", "c:copy", "x:cut");
    } else if (selectionCount > 0) {
      parts.push(`${selectionCount} selected`, "c:copy", "x:cut", "Esc:clear");
    } else {
      parts.push("/:filter", "Ctrl+F:search", "v:visual", "Space:select");
    }
    if (clipboard) {
      parts.push(`p:paste ${clipboard.paths.length} ${clipboard.operation === "cut" ? "cut" : "copied"}`);
    }
    parts.push("d:del", "N:mkdir", "R:rename");
    if (catalogAvailable) parts.push("m/a/s:meta");
    return parts.join("  ");
  }

  if (activeTab === "shareLinks") return "j/k:navigate  x:revoke  r:refresh  Tab:switch tab  q:quit";
  return "j/k:navigate  Tab:switch tab  q:quit";
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
  const fileCache = useFilesStore((s) => s.fileCache);
  const selectedIndex = useFilesStore((s) => s.selectedIndex);
  const toggleNode = useFilesStore((s) => s.toggleNode);
  const collapseNode = useFilesStore((s) => s.collapseNode);
  const setSelectedIndex = useFilesStore((s) => s.setSelectedIndex);
  const fetchPreview = useFilesStore((s) => s.fetchPreview);

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

  // Catalog brick availability
  const { available: catalogAvailable } = useBrickAvailable("catalog");

  // Active metadata sub-tab
  const [metadataTab, setMetadataTab] = React.useState<"metadata" | "aspects" | "schema">("metadata");
  React.useEffect(() => {
    if (!catalogAvailable && (metadataTab === "aspects" || metadataTab === "schema")) {
      setMetadataTab("metadata");
    }
  }, [catalogAvailable, metadataTab]);

  // Derived values
  const cachedFiles = fileCache.get(currentPath)?.data ?? [];
  const selectedItem: FileItem | null = cachedFiles[selectedIndex] ?? null;
  const visibleNodeCount = cachedFiles.length;

  // Aspect count badge
  const aspectsCache = useKnowledgeStore((s) => s.aspectsCache);
  const selectedUrn = selectedItem?.path && selectedItem?.zoneId
    ? `urn:nexus:file:${selectedItem.zoneId}:${crypto.createHash("sha256").update(selectedItem.path).digest("hex").slice(0, 32)}`
    : null;
  const aspectCount = selectedUrn ? (aspectsCache.get(selectedUrn)?.length ?? 0) : 0;

  // Clipboard copy (system)
  const { copy, copied } = useCopy();

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
    activeTab, cachedFiles, selectedIndex, selectedItem, visibleNodeCount,
    currentPath, client, setSelectedIndex, toggleNode, collapseNode,
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
  };

  useKeyboard(
    getKeyBindings(inputMode, overlayActive, confirmDelete, ctx),
    !overlayActive && inputMode !== "none" ? handleUnhandledKey : undefined,
  );

  const handleConfirmDelete = (): void => {
    setConfirmDelete(false);
    if (selectedItem && client) {
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
          <text foregroundColor="cyan">
            {pasteProgress.completed === pasteProgress.total
              ? `Paste complete: ${pasteProgress.completed}/${pasteProgress.total}${pasteProgress.failed > 0 ? ` (${pasteProgress.failed} failed)` : ""}`
              : `Pasting... ${pasteProgress.completed}/${pasteProgress.total}${pasteProgress.failed > 0 ? ` (${pasteProgress.failed} failed)` : ""}`}
          </text>
        </box>
      )}

      {/* Clipboard indicator (only when not actively pasting) */}
      {clipboard && !pasteProgress && inputMode === "none" && (
        <box height={1} width="100%">
          <text foregroundColor="yellow">
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
                  <text dimColor>Press Escape to return to explorer</text>
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
          ? <text foregroundColor="green">Copied!</text>
          : <text>
            {getHelpText(
              inputMode, activeTab, catalogAvailable,
              visualModeAnchor !== null, effectiveSelection.size,
              clipboard,
            )}
          </text>}
      </box>

      {/* Delete confirmation dialog */}
      <ConfirmDialog
        visible={confirmDelete}
        title="Delete File"
        message={`Delete "${selectedItem?.name ?? ""}"?`}
        onConfirm={handleConfirmDelete}
        onCancel={handleCancelDelete}
      />
    </box>
  );
}
