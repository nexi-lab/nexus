/**
 * Single row in the file list: icon + name + size + modified date.
 *
 * Wrapped with React.memo — re-renders only when item or selected changes.
 * @see Issue #3102, Decisions 4A + 5A
 */

import React from "react";
import type { FileItem } from "../../stores/files-store.js";
import { formatSize } from "../../shared/utils/format-size.js";

interface FileListItemProps {
  readonly item: FileItem;
  readonly selected: boolean;
}

export const FileListItem = React.memo(function FileListItem({ item, selected }: FileListItemProps): React.ReactNode {
  const icon = item.isDirectory ? "📁" : "📄";
  const prefix = selected ? "▸ " : "  ";
  const size = item.isDirectory ? "<DIR>" : formatSize(item.size);

  return (
    <box height={1} width="100%" flexDirection="row">
      <text>{`${prefix}${icon} ${item.name}`}</text>
      <text>{`  ${size}`}</text>
    </box>
  );
});
