import type { JSX } from "solid-js";
/**
 * File metadata sidebar displaying path, size, etag, version, owner, permissions, etc.
 */

import type { FileItem } from "../../stores/files-store.js";
import { textStyle } from "../../shared/text-style.js";
import { formatTimestamp } from "../../shared/utils/format-time.js";
import { statusColor } from "../../shared/theme.js";

interface FileMetadataProps {
  readonly item: FileItem | null;
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

function truncate(value: string | null | undefined, max: number = 30): string {
  if (value === null || value === undefined) return "n/a";
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function MetaRow(props: { label: string; value: string; color?: string }): JSX.Element {
  return (
    <box height={1} width="100%">
      <text>
        <span style={textStyle({ fg: statusColor.dim })}>{`${props.label.padEnd(8)} `}</span>
        <span style={props.color ? textStyle({ fg: props.color }) : undefined}>{props.value}</span>
      </text>
    </box>
  );
}

export function FileMetadata(props: FileMetadataProps): JSX.Element {
  // No if/return — unconditional rendering with ternary.
  // props.item is reactive via babel-preset-solid compiled getters.
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>{props.item ? `${props.item.name} — ${props.item.isDirectory ? "Directory" : "File"}` : "No file selected"}</text>
      <text>{props.item ? `Path: ${truncate(props.item.path, 50)}` : ""}</text>
      <text>{props.item ? `Size: ${formatBytes(props.item.size)}` : ""}</text>
      <text>{props.item ? `ETag: ${truncate(props.item.etag, 20)}` : ""}</text>
      <text>{props.item ? `Version: ${props.item.version ?? "n/a"}` : ""}</text>
      <text>{props.item ? `MIME: ${truncate(props.item.mimeType)}` : ""}</text>
      <text>{props.item ? `Owner: ${truncate(props.item.owner)}` : ""}</text>
      <text>{props.item ? `Perms: ${truncate(props.item.permissions)}` : ""}</text>
      <text>{props.item ? `Zone: ${props.item.zoneId ?? "n/a"}` : ""}</text>
      <text>{props.item ? `Modified: ${props.item.modifiedAt ? formatTimestamp(props.item.modifiedAt) : "n/a"}` : ""}</text>
    </box>
  );
}
