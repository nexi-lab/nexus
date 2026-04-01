/**
 * Keybinding builders and help-text helpers for the file explorer panel.
 *
 * Extracted from file-explorer-panel.tsx to separate keybinding logic
 * from component rendering (Decision 6A).
 *
 * @see Issue #3101 — filter/search, bulk ops, move/copy
 * @see Issue #3591 — split oversized TUI modules
 */

import {
  useFilesStore,
  type FileItem,
  type TreeNode,
  getEffectiveSelection,
} from "../../stores/files-store.js";
import {
  listNavigationBindings,
} from "../../shared/hooks/use-list-navigation.js";
import { subTabCycleBindings } from "../../shared/components/sub-tab-bar-utils.js";
import type { TabDef } from "../../shared/hooks/use-visible-tabs.js";
import type { useApi } from "../../shared/hooks/use-api.js";

// =============================================================================
// Panel-level tabs
// =============================================================================

export type FilesTab = "explorer" | "shareLinks" | "uploads";

// =============================================================================
// Input mode types
// =============================================================================

export type InputMode = "none" | "mkdir" | "rename" | "filter" | "search" | "paste-dest" | "create";

// =============================================================================
// Keybinding builders — one function per mode (Decision 6A)
// =============================================================================

export interface BindingContext {
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
  readonly setMetadataTab: (tab: "metadata" | "aspects" | "schema" | "lineage") => void;
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
    ...subTabCycleBindings(ctx.visibleTabs, ctx.activeTab, ctx.setActiveTab),
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
    "shift+l": () => ctx.setMetadataTab("lineage"),
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
export function getKeyBindings(
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

export function getInputLabel(mode: InputMode, buffer: string): string {
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
// Help bar text
// =============================================================================

export function getHelpText(
  inputMode: InputMode,
  activeTab: FilesTab,
  catalogAvailable: boolean,
  visualMode: boolean,
  selectionCount: number,
  clipboard: BindingContext["clipboard"],
): string {
  if (inputMode === "filter") return "Type to filter, Enter:keep filter, Escape:clear";
  if (inputMode === "search") return "g:pattern=glob  r:pattern=grep  plain=deep search  Enter:search  Esc:cancel";
  if (inputMode === "paste-dest") return "Enter path, Enter:paste, Escape:cancel";
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
      parts.push(`p:paste ${clipboard.paths.length} ${clipboard.operation === "cut" ? "cut" : "copied"}`, "P:paste to path");
    }
    parts.push("d:del", "N:mkdir", "R:rename", "e:edit", "E:new file");
    if (catalogAvailable) parts.push("m/a/s:meta");
    parts.push("?:help");
    return parts.join("  ");
  }

  if (activeTab === "shareLinks") return "j/k:navigate  x:revoke  r:refresh  Tab:switch tab  q:quit";
  return "j/k:navigate  Tab:switch tab  q:quit";
}
