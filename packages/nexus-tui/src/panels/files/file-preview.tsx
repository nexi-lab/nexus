/**
 * File content preview panel with syntax highlighting.
 */

import React, { useEffect } from "react";
import { useFilesStore } from "../../stores/files-store.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { Spinner } from "../../shared/components/spinner.js";
import { StyledText } from "../../shared/components/styled-text.js";

export function FilePreview(): React.ReactNode {
  const client = useApi();
  const previewPath = useFilesStore((s) => s.previewPath);
  const previewContent = useFilesStore((s) => s.previewContent);
  const previewLoading = useFilesStore((s) => s.previewLoading);
  const previewError = useFilesStore((s) => s.error);
  const fetchPreview = useFilesStore((s) => s.fetchPreview);

  useEffect(() => {
    if (client && previewPath) {
      fetchPreview(previewPath, client);
    }
  }, [client, previewPath, fetchPreview]);

  if (!previewPath) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Select a file to preview</text>
      </box>
    );
  }

  if (previewLoading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <Spinner label={`Loading ${previewPath}...`} />
      </box>
    );
  }

  if (previewContent === null) {
    return (
      <box height="100%" width="100%" flexDirection="column">
        <text>Unable to load preview</text>
        {previewError && (
          <text dimColor>{previewError}</text>
        )}
      </box>
    );
  }

  // Detect file extension for syntax highlighting
  const ext = previewPath.split(".").pop()?.toLowerCase() ?? "";
  const language = extensionToLanguage(ext);

  // Files that may contain ANSI escape sequences get rendered with StyledText
  const ansiExtensions = new Set(["log", "out", "err", "ans", "ansi"]);
  const hasAnsi = ansiExtensions.has(ext) || previewContent.includes("\x1b[");

  if (hasAnsi) {
    return (
      <scrollbox height="100%" width="100%">
        <StyledText>{previewContent}</StyledText>
      </scrollbox>
    );
  }

  // Use OpenTUI's Code component for syntax highlighting
  return (
    <scrollbox height="100%" width="100%">
      <code content={previewContent} filetype={language} />
    </scrollbox>
  );
}

function extensionToLanguage(ext: string): string {
  const map: Record<string, string> = {
    ts: "typescript",
    tsx: "tsx",
    js: "javascript",
    jsx: "jsx",
    py: "python",
    rs: "rust",
    go: "go",
    rb: "ruby",
    java: "java",
    c: "c",
    cpp: "cpp",
    h: "c",
    hpp: "cpp",
    json: "json",
    yaml: "yaml",
    yml: "yaml",
    toml: "toml",
    md: "markdown",
    sh: "bash",
    bash: "bash",
    zsh: "bash",
    sql: "sql",
    html: "html",
    css: "css",
    xml: "xml",
    proto: "protobuf",
  };
  return map[ext] ?? "text";
}
