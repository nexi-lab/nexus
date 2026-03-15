/**
 * Single row in the file list: icon + name + size + modified date.
 */

import React from "react";
import type { FileItem } from "../../stores/files-store.js";

interface FileListItemProps {
  readonly item: FileItem;
  readonly selected: boolean;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}K`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)}M`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)}G`;
}

export function FileListItem({ item, selected }: FileListItemProps): React.ReactNode {
  const icon = item.isDirectory ? "📁" : "📄";
  const prefix = selected ? "▸ " : "  ";
  const size = item.isDirectory ? "<DIR>" : formatSize(item.size);

  return (
    <box height={1} width="100%" flexDirection="row">
      <text>{`${prefix}${icon} ${item.name}`}</text>
      <text>{`  ${size}`}</text>
    </box>
  );
}
