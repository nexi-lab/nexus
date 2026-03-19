/**
 * File metadata sidebar displaying path, size, etag, version, owner, permissions, etc.
 */

import React from "react";
import * as crypto from "node:crypto";
import type { FileItem } from "../../stores/files-store.js";
import { formatTimestamp } from "../../shared/utils/format-time.js";

interface FileMetadataProps {
  readonly item: FileItem | null;
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

function display(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "n/a";
  if (typeof value === "number") return String(value);
  return value;
}

export function FileMetadata({ item }: FileMetadataProps): React.ReactNode {
  if (!item) {
    return (
      <box height="100%" width="100%">
        <text>No file selected</text>
      </box>
    );
  }

  const lines: string[] = [
    `Name: ${item.name}`,
    `Path: ${item.path}`,
    `Type: ${item.isDirectory ? "Directory" : "File"}`,
  ];

  if (!item.isDirectory) {
    lines.push(`Size: ${formatBytes(item.size)}`);
  }

  lines.push(`ETag: ${display(item.etag)}`);
  lines.push(`Version: ${display(item.version)}`);
  lines.push(`MIME: ${display(item.mimeType)}`);
  lines.push(`Owner: ${display(item.owner)}`);
  lines.push(`Permissions: ${display(item.permissions)}`);
  lines.push(`Zone: ${display(item.zoneId)}`);

  // URN (computed from path, matching NexusURN.for_file() in Python)
  if (item.path && item.zoneId) {
    const pathHash = crypto
      .createHash("sha256")
      .update(item.path)
      .digest("hex")
      .slice(0, 32);
    lines.push(`URN: urn:nexus:file:${item.zoneId}:${pathHash}`);
  }

  lines.push(`Modified: ${item.modifiedAt ? formatTimestamp(item.modifiedAt) : "n/a"}`);

  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>{"─── Metadata ───"}</text>
      {lines.map((line, i) => (
        <text key={i}>{line}</text>
      ))}
    </box>
  );
}
