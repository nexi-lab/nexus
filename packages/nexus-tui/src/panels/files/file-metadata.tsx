/**
 * File metadata sidebar displaying path, size, etag, timestamps, etc.
 */

import React from "react";
import type { FileItem } from "../../stores/files-store.js";

interface FileMetadataProps {
  readonly item: FileItem | null;
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
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

  if (item.etag) {
    lines.push(`ETag: ${item.etag}`);
  }

  if (item.mimeType) {
    lines.push(`MIME: ${item.mimeType}`);
  }

  if (item.modifiedAt) {
    lines.push(`Modified: ${item.modifiedAt}`);
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>{"─── Metadata ───"}</text>
      {lines.map((line, i) => (
        <text key={i}>{line}</text>
      ))}
    </box>
  );
}
