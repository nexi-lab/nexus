/**
 * File content preview panel with syntax highlighting.
 */

import { createEffect, createMemo, createSignal, onCleanup, Show } from "solid-js";
import type { JSX } from "solid-js";
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

export function FilePreview(): JSX.Element {
  const client = useApi();
  const fetchPreview = useFilesStore((s) => s.fetchPreview);

  // Subscribe to store for reactive updates (OpenTUI needs signal-driven rendering)
  const [previewPath, setPreviewPath] = createSignal(useFilesStore.getState().previewPath);
  const [previewContent, setPreviewContent] = createSignal(useFilesStore.getState().previewContent);
  const [previewLoading, setPreviewLoading] = createSignal(useFilesStore.getState().previewLoading);
  const [previewError, setPreviewError] = createSignal(useFilesStore.getState().error);
  const unsub = useFilesStore.subscribe((state) => {
    setPreviewPath(state.previewPath);
    setPreviewContent(state.previewContent);
    setPreviewLoading(state.previewLoading);
    setPreviewError(state.error);
  });
  onCleanup(unsub);

  const ext = () => previewPath()?.split(".").pop()?.toLowerCase() ?? "";
  const language = () => extensionToLanguage(ext());

  const hasAnsi = createMemo(
    () => ANSI_EXTENSIONS.has(ext()) || (previewContent()?.includes("\x1b[") ?? false),
  );

  // Pretty-print JSON files for readability
  const displayContent = createMemo(() => {
    const raw = previewContent();
    if (!raw) return raw;
    if (ext() === "json") {
      try { return JSON.stringify(JSON.parse(raw), null, 2); } catch { /* not valid JSON */ }
    }
    return raw;
  });

  // Preview is triggered by the keyboard handler (Enter/l on a file).
  // Do NOT auto-fetch here — previewPath is already set by fetchPreview,
  // and re-fetching on signal change creates an infinite loop.

  return (
    <Show when={previewPath()} fallback={
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Select a file to preview</text>
      </box>
    }>
      <Show when={!previewLoading()} fallback={
        <box height="100%" width="100%" justifyContent="center" alignItems="center">
          <Spinner label={`Loading ${previewPath()}...`} />
        </box>
      }>
        <Show when={previewContent() !== null} fallback={
          <box height="100%" width="100%" flexDirection="column">
            <text>Unable to load preview</text>
            {previewError() && (
              <text style={textStyle({ dim: true })}>{previewError()}</text>
            )}
          </box>
        }>
          <Show when={!hasAnsi()} fallback={
            <scrollbox height="100%" width="100%">
              <StyledText>{displayContent()!}</StyledText>
            </scrollbox>
          }>
            <scrollbox height="100%" width="100%">
              <code content={displayContent()!} filetype={language()} syntaxStyle={defaultSyntaxStyle} />
            </scrollbox>
          </Show>
        </Show>
      </Show>
    </Show>
  );
}
