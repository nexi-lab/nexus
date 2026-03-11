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

export default function FileExplorerPanel(): React.ReactNode {
  const currentPath = useFilesStore((s) => s.currentPath);
  const setCurrentPath = useFilesStore((s) => s.setCurrentPath);
  const focusPane = useFilesStore((s) => s.focusPane);
  const fileCache = useFilesStore((s) => s.fileCache);
  const selectedIndex = useFilesStore((s) => s.selectedIndex);

  // Get selected file item for metadata display
  const cachedFiles = fileCache.get(currentPath)?.data ?? [];
  const selectedItem: FileItem | null = cachedFiles[selectedIndex] ?? null;

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

          {/* Metadata sidebar (bottom 30%) */}
          <box flexGrow={3} borderStyle="single">
            <FileMetadata item={selectedItem} />
          </box>
        </box>
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {"j/k:navigate  l/Enter:expand  h:collapse  Tab:switch pane  /:search  q:quit"}
        </text>
      </box>
    </box>
  );
}
