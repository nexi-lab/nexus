/**
 * Full-screen file editor using OpenTUI's <textarea> component.
 *
 * Opens when pressing 'e' on a file in the explorer.
 * - Loads file content from the server
 * - Multi-line editing with undo/redo, cursor nav, selection
 * - Ctrl+S or Meta+Enter to save
 * - Esc to cancel
 */

import React, { useEffect, useRef, useState, useCallback } from "react";
import type { TextareaRenderable } from "@opentui/core";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useFilesStore } from "../../stores/files-store.js";
import { useVersionsStore } from "../../stores/versions-store.js";
import { Spinner } from "../../shared/components/spinner.js";
import { palette } from "../../shared/theme.js";

const EDITOR_BG = "#1a1a2e";
const EDITOR_SELECTION = "#264f78";

interface FileEditorProps {
  readonly path: string;
  readonly onClose: () => void;
}

export function FileEditor({ path, onClose }: FileEditorProps): React.ReactNode {
  const client = useApi();
  const textareaRef = useRef<TextareaRenderable>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [initialContent, setInitialContent] = useState("");
  // Must be called at top level (before any conditional returns) to respect Rules of Hooks
  const activeTxn = useVersionsStore((s) => s.selectedTransaction);
  const hasTxn = activeTxn?.status === "active";

  // Load file content
  useEffect(() => {
    if (!client) return;
    setLoading(true);
    setError(null);

    client.get<{ content: string }>(`/api/v2/files/read?path=${encodeURIComponent(path)}&include_metadata=false`)
      .then((response) => {
        const content = typeof response === "string" ? response : (response?.content ?? "");
        setInitialContent(content);
        if (textareaRef.current) {
          textareaRef.current.setText(content);
        }
        setLoading(false);
      })
      .catch((err) => {
        // New file — start with empty content
        setInitialContent("");
        if (textareaRef.current) {
          textareaRef.current.setText("");
        }
        setLoading(false);
      });
  }, [client, path]);

  // Set initial content once textarea mounts
  useEffect(() => {
    if (!loading && textareaRef.current && initialContent) {
      textareaRef.current.setText(initialContent);
    }
  }, [loading, initialContent]);

  // Save file (with optional transaction tracking)
  const handleSave = useCallback(async () => {
    if (!client || saving) return;
    const content = textareaRef.current?.plainText ?? "";
    setSaving(true);
    setError(null);
    try {
      // If an active transaction exists, pass its ID so the write is tracked
      const activeTxn = useVersionsStore.getState().selectedTransaction;
      const txnParam = activeTxn?.status === "active" ? `?transaction_id=${activeTxn.transaction_id}` : "";
      await client.post(`/api/v2/files/write${txnParam}`, {
        path,
        content,
      });
      setDirty(false);
      // Refresh parent directory in file tree
      const parentDir = path.split("/").slice(0, -1).join("/") || "/";
      useFilesStore.getState().invalidate(parentDir);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }, [client, path, saving, onClose]);

  // Keyboard shortcuts (only when not focused on textarea — textarea handles its own keys)
  useKeyboard({
    "escape": () => {
      onClose();
    },
    "ctrl+s": () => {
      handleSave();
    },
  });

  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <Spinner label={`Loading ${path}...`} />
      </box>
    );
  }

  const fileName = path.split("/").pop() ?? path;

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={1} width="100%">
        <text>
          <span foregroundColor={palette.accent} bold>{` ${fileName}`}</span>
          <span foregroundColor={palette.muted}>{` — ${path}`}</span>
          {dirty ? <span foregroundColor={palette.warning}>{" [modified]"}</span> : ""}
          {saving ? <span foregroundColor={palette.warning}>{" saving..."}</span> : ""}
          {hasTxn ? <span foregroundColor={palette.success}>{` [txn:${activeTxn!.transaction_id.slice(0, 8)}]`}</span> : ""}
        </text>
      </box>

      {/* Editor */}
      <box flexGrow={1} borderStyle="single" borderColor={dirty ? palette.warning : palette.faint}>
        <textarea
          ref={textareaRef}
          initialValue={initialContent}
          placeholder="Start typing..."
          wrapMode="word"
          focusedTextColor={palette.title}
          focusedBackgroundColor={EDITOR_BG}
          textColor={palette.bright}
          cursorColor={palette.accent}
          selectionBg={EDITOR_SELECTION}
          focused
          onContentChange={() => setDirty(true)}
          onSubmit={() => handleSave()}
        />
      </box>

      {/* Footer */}
      <box height={1} width="100%">
        <text>
          <span foregroundColor={palette.success} bold>{"  Ctrl+S"}</span>
          <span foregroundColor={palette.muted}>{":save  "}</span>
          <span foregroundColor={palette.error} bold>{"Esc"}</span>
          <span foregroundColor={palette.muted}>{":cancel  "}</span>
          <span foregroundColor={palette.accent}>{"Meta+Enter"}</span>
          <span foregroundColor={palette.muted}>{":save  "}</span>
          {error ? <span foregroundColor={palette.error}>{`  Error: ${error}`}</span> : ""}
        </text>
      </box>
    </box>
  );
}
