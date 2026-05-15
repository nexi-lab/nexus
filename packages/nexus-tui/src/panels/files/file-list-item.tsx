import type { JSX } from "solid-js";
/**
 * Single row in the file list: icon + name + size + modified date.
 *
 * Wrapped with React.memo — re-renders only when item or selected changes.
 * @see Issue #3102, Decisions 4A + 5A
 */

import type { FileItem } from "../../stores/files-store.js";
import { formatSize } from "../../shared/utils/format-size.js";

interface FileListItemProps {
  readonly item: FileItem;
  readonly selected: boolean;
}

export function FileListItem(props: FileListItemProps): JSX.Element {
  const icon = () => props.item.isDirectory ? "📁" : "📄";
  const prefix = () => props.selected ? "▸ " : "  ";
  const size = () => props.item.isDirectory ? "<DIR>" : formatSize(props.item.size);

  return (
    <box height={1} width="100%" flexDirection="row">
      <text>{`${prefix()}${icon()} ${props.item.name}`}</text>
      <text>{`  ${size()}`}</text>
    </box>
  );
}
