import { createSignal, Show } from "solid-js";
import type { TextareaRenderable } from "@opentui/core";
import type { VfsClient } from "@nexus-ai-fs/api-client";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useRequestScope } from "../../shared/hooks/use-request-scope.js";
import { ConfirmDialog } from "../../shared/components/confirm-dialog.js";

export function FileEditor(props: { readonly path: string; readonly content: string;
  readonly files: VfsClient; readonly onClose: () => void; readonly onSaved: () => void }) {
  let editor: TextareaRenderable | undefined;
  const [draft, setDraft] = createSignal(props.content);
  const [saved, setSaved] = createSignal(props.content);
  const dirty = () => draft() !== saved();
  const [discard, setDiscard] = createSignal(false);
  const scope = useRequestScope();
  const save = () => {
    if (!editor || scope.pending()) return;
    const content = editor.plainText;
    void scope.run((signal) => props.files.write(props.path, content, { signal }), () => {
      setSaved(content); props.onSaved();
    });
  };
  useKeyboard(() => discard() ? {} : {
    "ctrl+s": save,
    escape: () => { if (!scope.pending()) { if (dirty()) setDiscard(true); else props.onClose(); } },
  });
  return <box width="100%" height="100%" flexDirection="column">
    <text height={1} flexShrink={0}>{`${props.path}${dirty() ? " [modified]" : ""} | Ctrl+S:save  Esc:close`}</text>
    <Show when={!discard()} fallback={<ConfirmDialog visible title="Discard changes" message={props.path}
      onConfirm={props.onClose} onCancel={() => setDiscard(false)} />}>
      <textarea ref={(value) => { editor = value; }} initialValue={draft()} flexGrow={1} focused
        onContentChange={() => setDraft(editor?.plainText ?? draft())} onSubmit={save} />
    </Show>
    <Show when={scope.pending()}><text height={1} flexShrink={0}>{"Saving…"}</text></Show>
    <Show when={scope.error()}><text height={1} flexShrink={0}>{`Save failed: ${scope.error()}`}</text></Show>
  </box>;
}
