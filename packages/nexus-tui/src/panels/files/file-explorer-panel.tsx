/**
 * Full file explorer layout: left pane (tree) + right pane (preview/metadata).
 *
 * This is the main files panel, loaded lazily by the app.
 *
 * Panel-level tabs: Explorer | Share Links | Uploads
 */

import React, { useState, useCallback, useEffect } from "react";
import { useFilesStore, type FileItem } from "../../stores/files-store.js";
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
import { useApi } from "../../shared/hooks/use-api.js";
import { useBrickAvailable } from "../../shared/hooks/use-brick-available.js";
import { useVisibleTabs, type TabDef } from "../../shared/hooks/use-visible-tabs.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import crypto from "node:crypto";

// =============================================================================
// Panel-level tabs
// =============================================================================

type FilesTab = "explorer" | "shareLinks" | "uploads";

const ALL_TABS: readonly TabDef<FilesTab>[] = [
  { id: "explorer", label: "Explorer", brick: null },
  { id: "shareLinks", label: "Share Links", brick: null },
  { id: "uploads", label: "Uploads", brick: null },
];

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
  const focusPane = useFilesStore((s) => s.focusPane);
  const fileCache = useFilesStore((s) => s.fileCache);
  const selectedIndex = useFilesStore((s) => s.selectedIndex);
  const toggleNode = useFilesStore((s) => s.toggleNode);
  const collapseNode = useFilesStore((s) => s.collapseNode);
  const setSelectedIndex = useFilesStore((s) => s.setSelectedIndex);
  const setFocusPane = useFilesStore((s) => s.setFocusPane);
  const fetchPreview = useFilesStore((s) => s.fetchPreview);
  const deleteFile = useFilesStore((s) => s.deleteFile);
  const mkdirFile = useFilesStore((s) => s.mkdirFile);
  const renameFile = useFilesStore((s) => s.renameFile);

  // Share link store
  const shareLinks = useShareLinkStore((s) => s.links);
  const shareLinksLoading = useShareLinkStore((s) => s.linksLoading);
  const selectedLinkIndex = useShareLinkStore((s) => s.selectedLinkIndex);
  const fetchLinks = useShareLinkStore((s) => s.fetchLinks);
  const setSelectedLinkIndex = useShareLinkStore((s) => s.setSelectedLinkIndex);

  // Upload store
  const uploadSessions = useUploadStore((s) => s.sessions);
  const uploadsLoading = useUploadStore((s) => s.sessionsLoading);
  const selectedSessionIndex = useUploadStore((s) => s.selectedSessionIndex);
  const fetchSessions = useUploadStore((s) => s.fetchSessions);
  const setSelectedSessionIndex = useUploadStore((s) => s.setSelectedSessionIndex);

  // Get selected file item for metadata display
  const cachedFiles = fileCache.get(currentPath)?.data ?? [];
  const selectedItem: FileItem | null = cachedFiles[selectedIndex] ?? null;

  // Get visible node count for bounds checking
  const visibleNodeCount = cachedFiles.length;

  // Check if catalog brick is available for aspects/schema tabs
  const { available: catalogAvailable } = useBrickAvailable("catalog");

  // Active metadata sub-tab
  const [metadataTab, setMetadataTab] = React.useState<
    "metadata" | "aspects" | "schema"
  >("metadata");

  // Fall back to metadata tab if catalog becomes unavailable
  React.useEffect(() => {
    if (!catalogAvailable && (metadataTab === "aspects" || metadataTab === "schema")) {
      setMetadataTab("metadata");
    }
  }, [catalogAvailable, metadataTab]);

  // Aspect count badge for the selected file
  const aspectsCache = useKnowledgeStore((s) => s.aspectsCache);
  const selectedUrn = selectedItem?.path && selectedItem?.zoneId
    ? `urn:nexus:file:${selectedItem.zoneId}:${crypto.createHash("sha256").update(selectedItem.path).digest("hex").slice(0, 32)}`
    : null;
  const aspectCount = selectedUrn ? (aspectsCache.get(selectedUrn)?.length ?? 0) : 0;

  // Delete confirm dialog
  const [confirmDelete, setConfirmDelete] = useState(false);

  // Input mode for mkdir / rename
  const [inputMode, setInputMode] = useState<"none" | "mkdir" | "rename">("none");
  const [inputBuffer, setInputBuffer] = useState("");

  // Fetch share links / uploads when switching to those tabs
  useEffect(() => {
    if (!client) return;
    if (activeTab === "shareLinks") {
      fetchLinks(client);
    } else if (activeTab === "uploads") {
      fetchSessions(client);
    }
  }, [activeTab, client, fetchLinks, fetchSessions]);

  // Handle unhandled keys for input mode
  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      if (inputMode === "none") return;
      if (keyName.length === 1) {
        setInputBuffer((b) => b + keyName);
      } else if (keyName === "space") {
        setInputBuffer((b) => b + " ");
      }
    },
    [inputMode],
  );

  useKeyboard(
    confirmDelete
      ? {}  // ConfirmDialog handles its own keys
      : inputMode !== "none"
        ? {
            // Input mode: capture keystrokes for mkdir / rename
            return: () => {
              const value = inputBuffer.trim();
              if (!value || !client) {
                setInputMode("none");
                setInputBuffer("");
                return;
              }

              if (inputMode === "mkdir") {
                const dirPath = currentPath === "/" ? `/${value}` : `${currentPath}/${value}`;
                mkdirFile(dirPath, client);
              } else if (inputMode === "rename" && selectedItem) {
                const parentPath = selectedItem.path.split("/").slice(0, -1).join("/") || "/";
                const newPath = parentPath === "/" ? `/${value}` : `${parentPath}/${value}`;
                renameFile(selectedItem.path, newPath, client);
              }

              setInputMode("none");
              setInputBuffer("");
            },
            escape: () => {
              setInputMode("none");
              setInputBuffer("");
            },
            backspace: () => {
              setInputBuffer((b) => b.slice(0, -1));
            },
          }
        : {
            // Normal mode
            "j": () => {
              if (activeTab === "explorer") {
                setSelectedIndex(Math.min(selectedIndex + 1, visibleNodeCount - 1));
              } else if (activeTab === "shareLinks") {
                setSelectedLinkIndex(Math.min(selectedLinkIndex + 1, shareLinks.length - 1));
              } else if (activeTab === "uploads") {
                setSelectedSessionIndex(Math.min(selectedSessionIndex + 1, uploadSessions.length - 1));
              }
            },
            "down": () => {
              if (activeTab === "explorer") {
                setSelectedIndex(Math.min(selectedIndex + 1, visibleNodeCount - 1));
              } else if (activeTab === "shareLinks") {
                setSelectedLinkIndex(Math.min(selectedLinkIndex + 1, shareLinks.length - 1));
              } else if (activeTab === "uploads") {
                setSelectedSessionIndex(Math.min(selectedSessionIndex + 1, uploadSessions.length - 1));
              }
            },
            "k": () => {
              if (activeTab === "explorer") {
                setSelectedIndex(Math.max(selectedIndex - 1, 0));
              } else if (activeTab === "shareLinks") {
                setSelectedLinkIndex(Math.max(selectedLinkIndex - 1, 0));
              } else if (activeTab === "uploads") {
                setSelectedSessionIndex(Math.max(selectedSessionIndex - 1, 0));
              }
            },
            "up": () => {
              if (activeTab === "explorer") {
                setSelectedIndex(Math.max(selectedIndex - 1, 0));
              } else if (activeTab === "shareLinks") {
                setSelectedLinkIndex(Math.max(selectedLinkIndex - 1, 0));
              } else if (activeTab === "uploads") {
                setSelectedSessionIndex(Math.max(selectedSessionIndex - 1, 0));
              }
            },
            "return": () => {
              if (activeTab !== "explorer") return;
              const item = cachedFiles[selectedIndex];
              if (item && client) {
                if (item.isDirectory) {
                  toggleNode(item.path, client);
                } else {
                  fetchPreview(item.path, client);
                }
              }
            },
            "l": () => {
              if (activeTab !== "explorer") return;
              const item = cachedFiles[selectedIndex];
              if (item?.isDirectory && client) {
                toggleNode(item.path, client);
              }
            },
            "h": () => {
              if (activeTab !== "explorer") return;
              const item = cachedFiles[selectedIndex];
              if (item?.isDirectory) {
                collapseNode(item.path);
              }
            },
            "tab": () => {
              if (activeTab === "explorer") {
                // Tab cycles panel-level tabs
                const ids = visibleTabs.map((t) => t.id);
                const idx = ids.indexOf(activeTab);
                const next = ids[(idx + 1) % ids.length];
                if (next) setActiveTab(next);
              } else {
                // From other tabs, cycle back
                const ids = visibleTabs.map((t) => t.id);
                const idx = ids.indexOf(activeTab);
                const next = ids[(idx + 1) % ids.length];
                if (next) setActiveTab(next);
              }
            },
            "m": () => { if (activeTab === "explorer") setMetadataTab("metadata"); },
            "a": () => { if (activeTab === "explorer" && catalogAvailable) setMetadataTab("aspects"); },
            "s": () => { if (activeTab === "explorer" && catalogAvailable) setMetadataTab("schema"); },
            "d": () => {
              // Delete selected file (explorer tab)
              if (activeTab === "explorer" && selectedItem) {
                setConfirmDelete(true);
              }
            },
            "N": () => {
              // Shift+N: create new directory
              if (activeTab === "explorer") {
                setInputMode("mkdir");
                setInputBuffer("");
              }
            },
            "R": () => {
              // Shift+R: rename selected file
              if (activeTab === "explorer" && selectedItem) {
                setInputMode("rename");
                setInputBuffer(selectedItem.name);
              }
            },
          },
    inputMode !== "none" ? handleUnhandledKey : undefined,
  );

  const handleConfirmDelete = (): void => {
    setConfirmDelete(false);
    if (selectedItem && client) {
      deleteFile(selectedItem.path, client);
    }
  };

  const handleCancelDelete = (): void => {
    setConfirmDelete(false);
  };

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

      {/* Input bar for mkdir / rename */}
      {inputMode !== "none" && (
        <box height={1} width="100%">
          <text>
            {inputMode === "mkdir"
              ? `New directory: ${inputBuffer}\u2588`
              : `Rename to: ${inputBuffer}\u2588`}
          </text>
        </box>
      )}

      {/* Explorer tab */}
      {activeTab === "explorer" && (
        <box flexGrow={1} flexDirection="column">
          {/* Breadcrumb navigation */}
          <Breadcrumb path={currentPath} onNavigate={setCurrentPath} />

          {/* Main content: tree + preview */}
          <box flexGrow={1} flexDirection="row">
            {/* Left pane: file tree (40%) */}
            <box width="40%" height="100%" borderStyle="single">
              <FileTree />
            </box>

            {/* Right pane: preview + metadata (60%) */}
            <box width="60%" height="100%" flexDirection="column">
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
            loading={uploadsLoading}
          />
        </box>
      )}

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {inputMode !== "none"
            ? "Type name, Enter:confirm, Escape:cancel, Backspace:delete"
            : activeTab === "explorer"
              ? catalogAvailable
                ? "j/k:nav  l/Enter:expand  h:collapse  Tab:tab  m/a/s:meta  d:delete  N:mkdir  R:rename  q:quit"
                : "j/k:nav  l/Enter:expand  h:collapse  Tab:tab  m:meta  d:delete  N:mkdir  R:rename  q:quit"
              : activeTab === "shareLinks"
                ? "j/k:navigate  Tab:switch tab  n:create  q:quit"
                : "j/k:navigate  Tab:switch tab  r:refresh  q:quit"}
        </text>
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
