/**
 * File metadata sidebar displaying path, size, etag, version, owner, permissions, etc.
 */

import React from "react";
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

function MetaRow({ label, value, color }: { label: string; value: string; color?: string }): React.ReactNode {
  return (
    <box height={1} width="100%">
      <text>
        <span style={textStyle({ fg: statusColor.dim })}>{`${label.padEnd(8)} `}</span>
        <span style={color ? textStyle({ fg: color }) : undefined}>{value}</span>
      </text>
    </box>
  );
}

export function FileMetadata({ item }: FileMetadataProps): React.ReactNode {
  if (!item) {
    return (
      <box height="100%" width="100%">
        <text>No file selected</text>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      <MetaRow label="Name" value={item.name} color={statusColor.info} />
      <MetaRow label="Path" value={truncate(item.path, 40)} color={statusColor.reference} />
      <MetaRow label="Type" value={item.isDirectory ? "Directory" : "File"} />
      {!item.isDirectory && <MetaRow label="Size" value={formatBytes(item.size)} />}
      <MetaRow label="ETag" value={truncate(item.etag, 20)} />
      <MetaRow label="Version" value={truncate(item.version != null ? String(item.version) : null)} />
      <MetaRow label="MIME" value={truncate(item.mimeType)} />
      <MetaRow label="Owner" value={truncate(item.owner)} />
      <MetaRow label="Perms" value={truncate(item.permissions)} />
      <MetaRow label="Zone" value={item.zoneId ?? "n/a"} color={item.zoneId ? statusColor.reference : undefined} />
      <MetaRow label="Modified" value={item.modifiedAt ? formatTimestamp(item.modifiedAt) : "n/a"} />
    </box>
  );
}
