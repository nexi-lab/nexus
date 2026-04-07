/**
 * Full-screen file editor using OpenTUI's <textarea> component.
 *
 * Opens when pressing 'e' on a file in the explorer.
 * - Loads file content from the server
 * - Multi-line editing with undo/redo, cursor nav, selection
 * - Ctrl+S or Meta+Enter to save
 * - Esc to cancel
 */

import { createEffect, createSignal } from "solid-js";
import type { JSX } from "solid-js";
import type { TextareaRenderable } from "@opentui/core";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useFilesStore } from "../../stores/files-store.js";
import { useVersionsStore } from "../../stores/versions-store.js";
import { Spinner } from "../../shared/components/spinner.js";
import { textStyle } from "../../shared/text-style.js";

interface FileEditorProps {
  readonly path: string;
  readonly onClose: () => void;
}

export function FileEditor({ path, onClose }: FileEditorProps): JSX.Element {
  const client = useApi();
  let textareaRef: TextareaRenderable | null = null;
  const [loading, setLoading] = createSignal(true);
  const [saving, setSaving] = createSignal(false);
  const [error, setError] = createSignal<string | null>(null);
  const [dirty, setDirty] = createSignal(false);
  const [initialContent, setInitialContent] = createSignal("");
  // Must be called at top level (before any conditional returns) to respect Rules of Hooks
  const activeTxn = useVersionsStore((s) => s.selectedTransaction);
  const hasTxn = activeTxn?.status === "active";

  // Load file content
  createEffect(() => {
    if (!client) return;
    setLoading(true);
    setError(null);

    client.get<{ content: string }>(`/api/v2/files/read?path=${encodeURIComponent(path)}&include_metadata=false`)
      .then((response) => {
        const content = typeof response === "string" ? response : (response?.content ?? "");
        setInitialContent(content);
        if (textareaRef) {
          textareaRef.setText(content);
        }
        setLoading(false);
      })
      .catch((err) => {
        // New file — start with empty content
        setInitialContent("");
        if (textareaRef) {
          textareaRef.setText("");
        }
        setLoading(false);
      });
  });

  // Set initial content once textarea mounts
  createEffect(() => {
    if (!loading && textareaRef && initialContent) {
      textareaRef.setText(initialContent());
    }
  });

  // Save file (with optional transaction tracking)
  const handleSave = async () => {
    if (!client || saving()) return;
    const content = textareaRef?.plainText ?? "";
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
  };

  // Keyboard shortcuts (only when not focused on textarea — textarea handles its own keys)
  useKeyboard({
    "escape": () => {
      onClose();
    },
    "ctrl+s": () => {
      handleSave();
    },
  });

  if (loading()) {
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
          <span style={textStyle({ fg: "#00d4ff", bold: true })}>{` ${fileName}`}</span>
          <span style={textStyle({ fg: "#666666" })}>{` — ${path}`}</span>
          {dirty() ? <span style={textStyle({ fg: "#ffaa00" })}>{" [modified]"}</span> : ""}
          {saving() ? <span style={textStyle({ fg: "#ffaa00" })}>{" saving..."}</span> : ""}
          {hasTxn ? <span style={textStyle({ fg: "#4dff88" })}>{` [txn:${activeTxn!.transaction_id.slice(0, 8)}]`}</span> : ""}
        </text>
      </box>

      {/* Editor */}
      <box flexGrow={1} borderStyle="single" borderColor={dirty() ? "#ffaa00" : "#444444"}>
        <textarea
          ref={(el) => {
            textareaRef = el;
          }}
          initialValue={initialContent()}
          placeholder="Start typing..."
          wrapMode="word"
          focusedTextColor="#ffffff"
          focusedBackgroundColor="#1a1a2e"
          textColor="#cccccc"
          cursorColor="#00d4ff"
          selectionBg="#264f78"
          focused
          onContentChange={() => setDirty(true)}
          onSubmit={() => handleSave()}
        />
      </box>

      {/* Footer */}
      <box height={1} width="100%">
        <text>
          <span style={textStyle({ fg: "#4dff88", bold: true })}>{"  Ctrl+S"}</span>
          <span style={textStyle({ fg: "#888888" })}>{":save  "}</span>
          <span style={textStyle({ fg: "#ff4444", bold: true })}>{"Esc"}</span>
          <span style={textStyle({ fg: "#888888" })}>{":cancel  "}</span>
          <span style={textStyle({ fg: "#00d4ff" })}>{"Meta+Enter"}</span>
          <span style={textStyle({ fg: "#888888" })}>{":save  "}</span>
          {error ? <span style={textStyle({ fg: "#ff4444" })}>{`  Error: ${error}`}</span> : ""}
        </text>
      </box>
    </box>
  );
}
