/**
 * File content preview panel with syntax highlighting.
 */

import React, { useEffect, useMemo } from "react";
import { useFilesStore } from "../../stores/files-store.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { Spinner } from "../../shared/components/spinner.js";
import { StyledText } from "../../shared/components/styled-text.js";
import { textStyle } from "../../shared/text-style.js";
import { defaultSyntaxStyle } from "../../shared/syntax-style.js";

// Module-level constant: allocated once, not on every render call.
const EXTENSION_TO_LANGUAGE: Readonly<Record<string, string>> = {
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

// ANSI-bearing extensions — these are checked before the content scan.
const ANSI_EXTENSIONS = new Set(["log", "out", "err", "ans", "ansi"]);

/** Map a file extension to a tree-sitter language name. Exported for testing. */
export function extensionToLanguage(ext: string): string {
  return EXTENSION_TO_LANGUAGE[ext] ?? "text";
}

export function FilePreview(): React.ReactNode {
  const client = useApi();
  const previewPath = useFilesStore((s) => s.previewPath);
  const previewContent = useFilesStore((s) => s.previewContent);
  const previewLoading = useFilesStore((s) => s.previewLoading);
  const previewError = useFilesStore((s) => s.error);
  const fetchPreview = useFilesStore((s) => s.fetchPreview);

  const ext = previewPath?.split(".").pop()?.toLowerCase() ?? "";
  const language = extensionToLanguage(ext);

  // Memoize the ANSI scan — O(n) on file content, only recalculate when
  // content or extension changes, not on arbitrary parent re-renders.
  // Must be before any early returns to satisfy Rules of Hooks.
  const hasAnsi = useMemo(
    () => ANSI_EXTENSIONS.has(ext) || (previewContent?.includes("\x1b[") ?? false),
    [ext, previewContent],
  );

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
          <text style={textStyle({ dim: true })}>{previewError}</text>
        )}
      </box>
    );
  }

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
      <code content={previewContent} filetype={language} syntaxStyle={defaultSyntaxStyle} />
    </scrollbox>
  );
}
