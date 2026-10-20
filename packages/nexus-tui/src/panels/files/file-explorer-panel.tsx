import { createSignal, onCleanup, Show } from "solid-js";
import { VfsClient, type FileMetadata } from "#nexus-api-client";
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useRequestScope } from "../../shared/hooks/use-request-scope.js";
import { NativePrompt } from "../../shared/components/native-prompt.js";
import { ConfirmDialog } from "../../shared/components/confirm-dialog.js";
import { VirtualList } from "../../shared/components/virtual-list.js";
import { terminalDimensions } from "../../shared/terminal-dimensions.js";
import { FileEditor } from "./file-editor.js";

interface Prompt { label: string; initialValue: string; submit: (value: string) => void }
interface Preview { path: string; bytes: number; text: string | null }
const PREVIEW_BYTES = 64 * 1024;
const EDIT_BYTES = 1024 * 1024;
export default function FileExplorerPanel(props: { readonly onInputMode: (active: boolean) => void }) {
  const files = new VfsClient(useGlobalStore.getState().client!);
  const scope = useRequestScope();
  const [path, setPath] = createSignal("/");
  const [entries, setEntries] = createSignal<readonly FileMetadata[]>([]);
  const [selected, setSelected] = createSignal(0);
  const [preview, setPreview] = createSignal<Preview | null>(null);
  const [editing, setEditing] = createSignal<{ path: string; content: string } | null>(null);
  const [prompt, setPrompt] = createSignal<Prompt | null>(null);
  const [deleting, setDeleting] = createSignal<FileMetadata | null>(null);
  const [notice, setNotice] = createSignal("");
  onCleanup(() => props.onInputMode(false));
  const load = (destination = path()) => {
    if (scope.pending()) return;
    setEntries([]); setPreview(null); setNotice("");
    void scope.run((signal) => files.list(destination, { signal }), (result) => {
      setPath(destination); setSelected(0);
      setEntries([...result].sort((a, b) => Number(b.is_directory) - Number(a.is_directory) || a.path.localeCompare(b.path)));
    });
  };
  const ask = (label: string, initialValue: string, submit: (value: string) => void) => {
    if (scope.pending()) return;
    props.onInputMode(true); setPrompt({ label, initialValue, submit });
  };
  const closePrompt = () => { setPrompt(null); props.onInputMode(false); };
  const openEditor = (file: { path: string; content: string }) => {
    props.onInputMode(true); setEditing(file);
  };
  const read = (entry: FileMetadata, edit: boolean) => {
    if (entry.is_directory) { load(entry.path); return; }
    void scope.run((signal) => files.read(entry.path, { signal }), (bytes) => {
      const canEdit = edit && bytes.length <= EDIT_BYTES;
      const shown = canEdit ? bytes : bytes.subarray(0, PREVIEW_BYTES);
      let text: string | null = null;
      try {
        if (!shown.includes(0)) text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true })
          .decode(shown, { stream: shown.length < bytes.length });
      } catch { /* Display binary size. */ }
      if (canEdit && text !== null) openEditor({ path: entry.path, content: text });
      else {
        setPreview({ path: entry.path, bytes: bytes.length, text: text === null ? null
          : text + (shown.length < bytes.length ? "\n[preview truncated]" : "") });
        if (edit) setNotice(bytes.length > EDIT_BYTES ? "Text editing is limited to 1 MiB." : "Binary file; text editing unavailable.");
      }
    });
  };
  const current = () => entries()[selected()];
  useKeyboard(() => prompt() || editing() || deleting() ? {} : {
    j: () => setSelected(Math.min(selected() + 1, Math.max(entries().length - 1, 0))),
    k: () => setSelected(Math.max(selected() - 1, 0)),
    return: () => { const entry = current(); if (entry) read(entry, false); },
    e: () => { const entry = current(); if (entry) read(entry, true); },
    r: () => load(),
    backspace: () => load(path().replace(/\/+$/, "").slice(0, path().replace(/\/+$/, "").lastIndexOf("/")) || "/"),
    g: () => ask("Directory path", path(), load),
    n: () => ask("Write file path", `${path().replace(/\/$/, "")}/`, (destination) => openEditor({ path: destination, content: "" })),
    m: () => ask("Create directory", `${path().replace(/\/$/, "")}/`, (destination) => {
      void scope.run((signal) => files.mkdir(destination, { signal }), () => { setNotice("Directory created."); load(); });
    }),
    v: () => { const entry = current(); if (entry) ask("Rename to", entry.path, (destination) => {
      void scope.run((signal) => files.rename(entry.path, destination, { signal }), () => { setNotice("Renamed."); load(); });
    }); },
    d: () => { const entry = current(); if (entry && !scope.pending()) { props.onInputMode(true); setDeleting(entry); } },
  });
  load();
  return <box height="100%" width="100%" flexDirection="column">
    <Show when={!editing()} fallback={<Show when={editing()} keyed>{(file) => <FileEditor {...file} files={files}
      onSaved={() => setNotice("Saved native file.")} onClose={() => { setEditing(null); props.onInputMode(false); load(); }} />}</Show>}>
      <text height={1} flexShrink={0}>{`Files: ${path()} | j/k:select  Enter:open  e:edit  g:path  n:write  m:mkdir  v:rename  d:delete  r:refresh  Backspace:parent`}</text>
      <Show when={prompt()} keyed>{(value) => <NativePrompt label={value.label} initialValue={value.initialValue}
        onCancel={closePrompt} onSubmit={(text) => { closePrompt(); if (text.trim()) value.submit(text); }} />}</Show>
      <Show when={deleting()} keyed>{(entry) => <ConfirmDialog visible title="Delete native path" message={entry.path}
        onCancel={() => { setDeleting(null); props.onInputMode(false); }} onConfirm={() => {
          setDeleting(null); props.onInputMode(false);
          void scope.run(async (signal) => {
            if (!await files.delete(entry.path, entry.is_directory, { signal })) throw new Error("Path was not deleted");
          }, () => load());
        }} />}</Show>
      <Show when={scope.pending()}><text height={1} flexShrink={0}>{"Loading…"}</text></Show>
      <Show when={scope.error()}><text height={1} flexShrink={0}>{`Error: ${scope.error()}`}</text></Show>
      <Show when={notice()}><text height={1} flexShrink={0}>{notice()}</text></Show>
      <box width="100%" flexGrow={1} flexDirection="row">
        <box width="50%" height="100%"><VirtualList items={entries()} selectedIndex={selected()} viewportHeight={Math.max(1, terminalDimensions().height - 8)}
          renderItem={(entry, index) => <text height={1} flexShrink={0}>{`${index === selected() ? ">" : " "} ${entry.is_directory ? "DIR" : "FILE"} ${entry.path} (${entry.size} bytes)`}</text>} /></box>
        <box width="50%" height="100%" flexDirection="column"><Show when={preview()} keyed>{(value) => <>
          <text height={1} flexShrink={0}>{`${value.path} (${value.bytes} bytes)`}</text>
          <scrollbox flexGrow={1} minHeight={0}><text>{value.text === null ? "Binary file" : value.text}</text></scrollbox>
        </>}</Show></box>
      </box>
    </Show>
  </box>;
}
