/**
 * Full file explorer layout: left pane (tree) + right pane (preview/metadata).
 *
 * This is the main files panel, loaded lazily by the app.
 */

import React from "react";
import { useFilesStore, type FileItem } from "../../stores/files-store.js";
import { Breadcrumb } from "../../shared/components/breadcrumb.js";
import { FileTree } from "./file-tree.js";
import { FilePreview } from "./file-preview.js";
import { FileMetadata } from "./file-metadata.js";
import { FileAspects } from "./file-aspects.js";
import { FileSchema } from "./file-schema.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useBrickAvailable } from "../../shared/hooks/use-brick-available.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import crypto from "node:crypto";

export default function FileExplorerPanel(): React.ReactNode {
  const client = useApi();
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

  useKeyboard({
    "j": () => setSelectedIndex(Math.min(selectedIndex + 1, visibleNodeCount - 1)),
    "down": () => setSelectedIndex(Math.min(selectedIndex + 1, visibleNodeCount - 1)),
    "k": () => setSelectedIndex(Math.max(selectedIndex - 1, 0)),
    "up": () => setSelectedIndex(Math.max(selectedIndex - 1, 0)),
    "return": () => {
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
      const item = cachedFiles[selectedIndex];
      if (item?.isDirectory && client) {
        toggleNode(item.path, client);
      }
    },
    "h": () => {
      const item = cachedFiles[selectedIndex];
      if (item?.isDirectory) {
        collapseNode(item.path);
      }
    },
    "tab": () => setFocusPane(focusPane === "tree" ? "preview" : "tree"),
    "m": () => setMetadataTab("metadata"),
    "a": () => { if (catalogAvailable) setMetadataTab("aspects"); },
    "s": () => { if (catalogAvailable) setMetadataTab("schema"); },
  });

  return (
    <box height="100%" width="100%" flexDirection="column">
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

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {catalogAvailable
            ? "j/k:navigate  l/Enter:expand  h:collapse  Tab:pane  m/a/s:meta/aspects/schema  q:quit"
            : "j/k:navigate  l/Enter:expand  h:collapse  Tab:pane  m:metadata  q:quit"}
        </text>
      </box>
    </box>
  );
}
