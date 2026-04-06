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

import { createSignal, createEffect, createMemo, onCleanup } from "solid-js";
import type { JSX } from "solid-js";
import {
  useFilesStore,
  type FileItem,
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
import { FileLineage } from "./file-lineage.js";
import { FileSchema } from "./file-schema.js";
import { ShareLinksTab } from "./share-links-tab.js";
import { UploadsTab } from "./uploads-tab.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useBrickAvailable } from "../../shared/hooks/use-brick-available.js";
import { useVisibleTabs, type TabDef } from "../../shared/hooks/use-visible-tabs.js";
import { SubTabBar } from "../../shared/components/sub-tab-bar.js";
import { useTabFallback } from "../../shared/hooks/use-tab-fallback.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { useUiStore } from "../../stores/ui-store.js";
import { useAnnouncementStore } from "../../stores/announcement-store.js";
import { focusColor, statusColor } from "../../shared/theme.js";
import {
  formatDirectoryAnnouncement,
  formatSelectionAnnouncement,
  formatSuccessAnnouncement,
} from "../../shared/accessibility-announcements.js";
import crypto from "node:crypto";
import {
  getKeyBindings, getInputLabel, getHelpText,
} from "./file-explorer-keybindings.js";
import type { InputMode, BindingContext, FilesTab } from "./file-explorer-keybindings.js";

// =============================================================================
// Panel-level tabs
// =============================================================================

const ALL_TABS: readonly TabDef<FilesTab>[] = [
  { id: "explorer", label: "Explorer", brick: null },
  { id: "shareLinks", label: "Share Links", brick: "share_link" },
  { id: "uploads", label: "Uploads", brick: "uploads" },
];

// =============================================================================
// Component
// =============================================================================

export default function FileExplorerPanel(): JSX.Element {
  const client = useApi();
  const visibleTabs = useVisibleTabs(ALL_TABS);

  // Panel-level active tab
  const [activeTab, setActiveTab] = createSignal<FilesTab>("explorer");

  useTabFallback(visibleTabs, activeTab(), setActiveTab);

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
  onCleanup(() => { abortAll(); });

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
  const [metadataTab, setMetadataTab] = createSignal<"metadata" | "aspects" | "schema" | "lineage">("metadata");
  createEffect(() => {
    if (!catalogAvailable && (metadataTab() === "aspects" || metadataTab() === "schema")) {
      setMetadataTab("metadata");
    }
  });

  // Flattened visible tree nodes — the source of truth for explorer navigation.
  const visibleNodes = createMemo(
    () => flattenVisibleNodes(currentPath, treeNodes),
  );

  const selectedNode = visibleNodes()[selectedIndex] ?? null;
  const isSentinel = selectedNode?.path.endsWith(LOAD_MORE_SENTINEL) ?? false;
  const currentTreeNode = treeNodes.get(currentPath);
  let lastDirectoryAnnouncementRef: string | null = null;
  let lastSelectionAnnouncementRef: string | null = null;
  let lastPasteAnnouncementRef: string | null = null;

  // For metadata/actions, look up FileItem from parent's file cache first,
  // then fall back to constructing a minimal FileItem from the tree node.
  // The fallback ensures metadata pane works even if the file cache is empty
  // (e.g. requests were aborted during rapid navigation).
  const selectedItem = createMemo(() => {
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
  });

  const visibleNodeCount = visibleNodes.length;
  // Keep cachedFiles for backward compat with BindingContext (selection uses it)
  const cachedFiles = fileCacheRevision >= 0 ? (getCachedFiles(currentPath) ?? []) : [];

  createEffect(() => {
    lastSelectionAnnouncementRef = null;
  });

  createEffect(() => {
    if (pasteProgress === null) {
      lastPasteAnnouncementRef = null;
    }
  });

  createEffect(() => {
    if (!currentTreeNode || currentTreeNode.loading) return;
    const key = `${currentPath}:${cachedFiles.length}:${fileCacheRevision}`;
    if (lastDirectoryAnnouncementRef === key) return;
    lastDirectoryAnnouncementRef = key;
    announce(formatDirectoryAnnouncement(currentPath, cachedFiles.length));
  });

  createEffect(() => {
    if (!selectedNode || isSentinel) return;
    if (lastSelectionAnnouncementRef === null) {
      lastSelectionAnnouncementRef = selectedNode.path;
      return;
    }
    if (lastSelectionAnnouncementRef === selectedNode.path) return;
    lastSelectionAnnouncementRef = selectedNode.path;
    announce(formatSelectionAnnouncement(selectedNode.name, selectedNode.isDirectory));
  });

  createEffect(() => {
    if (!pasteProgress) return;
    const completed = pasteProgress.completed + pasteProgress.failed;
    if (completed < pasteProgress.total) return;
    const key = `${pasteProgress.total}:${pasteProgress.completed}:${pasteProgress.failed}:${clipboard?.operation ?? "none"}`;
    if (lastPasteAnnouncementRef === key) return;
    lastPasteAnnouncementRef = key;
    announce(
      formatSuccessAnnouncement(
        pasteProgress.failed > 0
          ? `Paste complete: ${pasteProgress.completed} succeeded, ${pasteProgress.failed} failed`
          : `Paste complete: ${pasteProgress.completed} items`,
      ),
      pasteProgress.failed > 0 ? "error" : "success",
    );
  });

  // Aspect count badge
  const aspectsCache = useKnowledgeStore((s) => s.aspectsCache);
  const selectedUrn = selectedItem()?.path
    ? `urn:nexus:file:${selectedItem()?.zoneId || "default"}:${crypto.createHash("sha256").update(selectedItem()!.path).digest("hex").slice(0, 32)}`
    : null;
  const aspectCount = selectedUrn ? (aspectsCache.get(selectedUrn)?.length ?? 0) : 0;

  // Clipboard copy (system)
  const { copy, copied } = useCopy();

  // Editor overlay state — suppress global panel-switch keys while editor is open
  const [editorPath, setEditorPath] = createSignal<string | null>(null);
  const openEditor = (path: string) => {
    useUiStore.getState().setFileEditorOpen(true);
    setEditorPath(path);
  };
  const closeEditor = () => {
    useUiStore.getState().setFileEditorOpen(false);
    setEditorPath(null);
  };

  // Dialog state
  const [confirmDelete, setConfirmDelete] = createSignal(false);

  // Input mode
  const [inputMode, setInputMode] = createSignal<InputMode>("none");
  const [inputBuffer, setInputBuffer] = createSignal("");

  // Filter & search state
  const [filterQuery, setFilterQuery] = createSignal("");
  const [searchQuery, setSearchQuery] = createSignal("");
  const [searchResults, setSearchResults] = createSignal<readonly { path: string; line?: number; content?: string }[] | null>(null);

  // Effective selection count for display
  const effectiveSelection = createMemo(() => {
    if (activeTab() !== "explorer") return new Set<string>();
    return getEffectiveSelection(
      selectedPaths, visualModeAnchor, selectedIndex,
      cachedFiles.map((f) => f.path),
    );
  });

  // Fetch share links when switching to that tab
  createEffect(() => {
    if (!client) return;
    if (activeTab() === "shareLinks") fetchLinks(client);
  });

  // Search execution
  const executeSearch = async (query: string) => {
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
  };

  // Build input buffer reference for the binding context
  // The input buffer needs to be passed through the context for mkdir/rename
  // to access the current value in their return handlers
  const inputBufferRef = inputMode() === "filter" ? filterQuery
    : inputMode() === "search" ? searchQuery
    : inputMode() === "paste-dest" ? inputBuffer
    : inputBuffer;

  // Handle unhandled keys for text input modes
  const handleUnhandledKey = (keyName: string) => {
      if (inputMode() === "none") return;
      const setter = inputMode() === "filter" ? setFilterQuery
        : inputMode() === "search" ? setSearchQuery
        : setInputBuffer;
      if (keyName.length === 1) {
        setter((b) => b + keyName);
      } else if (keyName === "space") {
        setter((b) => b + " ");
      }
    };

  // Build binding context — reads fresh store values on every call.
  // useKeyboard calls this function on each keypress, so ctx is always current.
  const buildCtx = (): BindingContext => {
    const state = useFilesStore.getState();
    const nodes = flattenVisibleNodes(state.currentPath, state.treeNodes);
    const selNode = nodes[state.selectedIndex] ?? null;
    const files = state.getCachedFiles(state.currentPath) ?? [];
    const selItem = files[state.selectedIndex] ?? null;
    return {
      activeTab: activeTab(), cachedFiles: files as readonly FileItem[],
      selectedIndex: state.selectedIndex, selectedItem: selItem,
      selectedNode: selNode, isSentinel: selNode?.path.endsWith(LOAD_MORE_SENTINEL) ?? false,
      visibleNodeCount: nodes.length, currentPath: state.currentPath,
      client, setSelectedIndex, toggleNode, collapseNode,
      fetchPreview, setMetadataTab, catalogAvailable,
      shareLinks: useShareLinkStore.getState().links,
      selectedLinkIndex: useShareLinkStore.getState().selectedLinkIndex,
      setSelectedLinkIndex, revokeLink, fetchLinks,
      uploadSessions: useUploadStore.getState().sessions,
      selectedSessionIndex: useUploadStore.getState().selectedSessionIndex,
      setSelectedSessionIndex,
      visibleTabs, setActiveTab, toggleFocus, copy, setConfirmDelete,
      setInputMode, setInputBuffer,
      selectedPaths: state.selectedPaths, visualModeAnchor: state.visualModeAnchor,
      clipboard: state.clipboard,
      toggleSelect, clearSelection, enterVisualMode, exitVisualMode,
      yankToClipboard, cutToClipboard, clearClipboard, pasteFiles,
      filterQuery: filterQuery(), setFilterQuery, searchQuery: searchQuery(), setSearchQuery,
      executeSearch,
      searchResults: searchResults(), setSearchResults,
      setInputModeWithCallback: setInputMode as BindingContext["setInputModeWithCallback"],
      openEditor,
    };
  };

  useKeyboard(
    () => {
      const im = inputMode();
      const ov = useUiStore.getState().overlayActive;
      const cd = confirmDelete();
      const eo = editorPath() !== null;
      const bindings = getKeyBindings(im, ov, cd, eo, buildCtx());
      return bindings;
    },
    !useUiStore.getState().overlayActive && inputMode() !== "none" && editorPath() === null ? handleUnhandledKey : undefined,
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
    } else if (selectedItem()) {
      useFilesStore.getState().deleteFile(selectedItem()!.path, client);
    }
  };

  const handleCancelDelete = (): void => {
    setConfirmDelete(false);
  };

  // Determine which input buffer to display
  const displayBuffer = inputMode() === "filter" ? filterQuery
    : inputMode() === "search" ? searchQuery
    : inputBuffer;

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Full-screen file editor */}
      {editorPath() ? (
        <FileEditor path={editorPath()!} onClose={closeEditor} />
      ) : <>

      {/* Panel-level tab bar */}
      <SubTabBar tabs={visibleTabs} activeTab={activeTab()} onSelect={setActiveTab as (id: string) => void} />

      {/* Input bar for text modes */}
      {inputMode() !== "none" && (
        <box height={1} width="100%">
          <text>{getInputLabel(inputMode(), displayBuffer())}</text>
        </box>
      )}

      {/* Paste progress indicator */}
      {pasteProgress && (
        <box height={1} width="100%">
          <text foregroundColor={statusColor.info}>
            {pasteProgress.completed + pasteProgress.failed >= pasteProgress.total
              ? `Paste complete: ${pasteProgress.completed}/${pasteProgress.total}${pasteProgress.failed > 0 ? ` (${pasteProgress.failed} failed)` : ""}`
              : `Pasting... ${pasteProgress.completed + pasteProgress.failed}/${pasteProgress.total}${pasteProgress.failed > 0 ? ` (${pasteProgress.failed} failed)` : ""}`}
          </text>
        </box>
      )}

      {/* Clipboard indicator (only when not actively pasting) */}
      {clipboard && !pasteProgress && inputMode() === "none" && (
        <box height={1} width="100%">
          <text foregroundColor={statusColor.warning}>
            {`${clipboard.paths.length} file${clipboard.paths.length > 1 ? "s" : ""} ${clipboard.operation === "cut" ? "cut" : "copied"} — press p to paste`}
          </text>
        </box>
      )}

      {/* Explorer tab */}
      {activeTab() === "explorer" && (
        <box flexGrow={1} flexDirection="column">
          {/* Breadcrumb navigation */}
          <Breadcrumb path={currentPath} onNavigate={setCurrentPath} />

          {/* Search results overlay */}
          {searchResults() !== null ? (
            <box flexGrow={1} borderStyle="single">
              <scrollbox height="100%" width="100%">
                {(searchResults()?.length ?? 0) === 0
                  ? <text>No results found</text>
                  : searchResults()!.map((result, i) => (
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
                  filterQuery={filterQuery()}
                  effectiveSelection={effectiveSelection()}
                />
              </box>

              {/* Right pane: preview + metadata (60%) */}
              <box width="60%" height="100%" flexDirection="column" borderStyle="single" borderColor={uiFocusPane === "right" ? focusColor.activeBorder : focusColor.inactiveBorder}>
                {/* File preview (top 70%) */}
                <box flexGrow={7} borderStyle="single">
                  <FilePreview />
                </box>

                {/* Metadata tab bar with aspect count badge */}
                <box height={1} width="100%" flexDirection="row">
                  <box height={1} onMouseDown={() => setMetadataTab("metadata")}>
                    <text>{metadataTab() === "metadata" ? " [Metadata]" : "  Metadata "}</text>
                  </box>
                  <box height={1} onMouseDown={() => setMetadataTab("lineage")}>
                    <text>{metadataTab() === "lineage" ? " [Lineage]" : "  Lineage "}</text>
                  </box>
                  {catalogAvailable && (
                    <box height={1} onMouseDown={() => setMetadataTab("aspects")}>
                      <text>{metadataTab() === "aspects" ? ` [Aspects${aspectCount > 0 ? ` (${aspectCount})` : ""}]` : `  Aspects${aspectCount > 0 ? ` (${aspectCount})` : ""}  `}</text>
                    </box>
                  )}
                  {catalogAvailable && (
                    <box height={1} onMouseDown={() => setMetadataTab("schema")}>
                      <text>{metadataTab() === "schema" ? " [Schema]" : "  Schema "}</text>
                    </box>
                  )}
                </box>

                {/* Metadata sidebar (bottom 30%) */}
                <box flexGrow={3} borderStyle="single">
                  {metadataTab() === "metadata" && <FileMetadata item={selectedItem()} />}
                  {metadataTab() === "lineage" && <FileLineage item={selectedItem()} />}
                  {metadataTab() === "aspects" && catalogAvailable && <FileAspects item={selectedItem()} />}
                  {metadataTab() === "schema" && catalogAvailable && <FileSchema item={selectedItem()} />}
                </box>
              </box>
            </box>
          )}
        </box>
      )}

      {/* Share Links tab */}
      {activeTab() === "shareLinks" && (
        <box flexGrow={1} borderStyle="single">
          <ShareLinksTab
            links={shareLinks}
            selectedIndex={selectedLinkIndex}
            loading={shareLinksLoading}
          />
        </box>
      )}

      {/* Uploads tab */}
      {activeTab() === "uploads" && (
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
          ? <text foregroundColor={statusColor.healthy}>Copied!</text>
          : <text>
            {getHelpText(
              inputMode(), activeTab(), catalogAvailable,
              visualModeAnchor !== null, effectiveSelection().size,
              clipboard,
            )}
          </text>}
      </box>

      {/* Delete confirmation dialog */}
      <ConfirmDialog
        visible={confirmDelete()}
        title={effectiveSelection().size > 0 ? "Delete Selected" : "Delete File"}
        message={effectiveSelection().size > 1
          ? `Delete ${effectiveSelection().size} selected files?`
          : effectiveSelection().size === 1
            ? `Delete "${[...effectiveSelection()][0]!.split("/").pop()}"?`
            : `Delete "${selectedItem()?.name ?? ""}"?`}
        onConfirm={handleConfirmDelete}
        onCancel={handleCancelDelete}
      />
    </>}
    </box>
  );
}
