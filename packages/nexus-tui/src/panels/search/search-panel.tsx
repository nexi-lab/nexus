import { createSignal, onCleanup, Show } from "solid-js";
import { useSearchStore } from "../../stores/search-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useRequestScope } from "../../shared/hooks/use-request-scope.js";
import { NativePrompt } from "../../shared/components/native-prompt.js";
import { SearchResults } from "./search-results.js";
import { searchFiles, type FileSearchResult } from "../files/file-search.js";

export default function SearchPanel(props: { readonly onInputMode: (active: boolean) => void }) {
  const [input, setInput] = createSignal(false);
  const [discovery, setDiscovery] = createSignal<FileSearchResult | null>(null);
  const [root, setRoot] = createSignal("/");
  const [rootInput, setRootInput] = createSignal(false);
  const scope = useRequestScope();
  const state = useSearchStore.getState;
  const config = useGlobalStore.getState().config;
  const client = useGlobalStore.getState().client!;
  const connection = { url: config.searchUrl, zoneId: config.zoneId };
  const transport = {
    get: <T,>(path: string, options?: import("#nexus-api-client").RequestOptions) => client.get<T>(path, { ...options, signal: scope.signal }),
    post: <T,>(path: string, body: unknown, options?: import("#nexus-api-client").RequestOptions) => client.post<T>(path, body, { ...options, signal: scope.signal }),
  };
  onCleanup(() => { state().cancel(); props.onInputMode(false); });
  const close = () => { setInput(false); setRootInput(false); props.onInputMode(false); };
  const submit = (query: string) => {
    if (scope.pending() || state().searchLoading) return;
    close(); setDiscovery(null);
    if (query.startsWith("g:") || query.startsWith("r:"))
      void scope.run(() => searchFiles(query, root(), transport, connection), setDiscovery);
    else if (query.trim()) void state().search(query, client, connection);
  };
  useKeyboard(() => input() || rootInput() ? {} : {
    "/": () => { if (!scope.pending() && !state().searchLoading) { setInput(true); props.onInputMode(true); } },
    g: () => { if (!scope.pending() && !state().searchLoading) { setRootInput(true); props.onInputMode(true); } },
    m: () => state().cycleSearchMode(), r: () => submit(state().searchQuery),
    j: () => state().setSelectedResultIndex(Math.min(state().selectedResultIndex + 1, Math.max(state().searchTotal - 1, 0))),
    k: () => state().setSelectedResultIndex(Math.max(state().selectedResultIndex - 1, 0)),
  });
  return <box height="100%" width="100%" flexDirection="column">
    <text height={1} flexShrink={0}>{`Search zone:${config.zoneId ?? "readable zones"} mode:${state().searchMode} | /:query  g:root  m:mode  j/k:select  r:refresh`}</text>
    <text height={1} flexShrink={0}>{`Glob g:pattern | Grep r:pattern | Discovery root: ${root()}`}</text>
    <Show when={input()}><NativePrompt label="Query" initialValue={state().searchQuery} onSubmit={submit} onCancel={close} /></Show>
    <Show when={rootInput()}><NativePrompt label="Discovery root" initialValue={root()} onSubmit={(value) => { setRoot(value); close(); }} onCancel={close} /></Show>
    <Show when={state().error || scope.error()}><text height={1} flexShrink={0}>{`Error: ${state().error ?? scope.error()}`}</text></Show>
    <Show when={discovery()} keyed fallback={<SearchResults results={state().searchResults} total={state().searchTotal}
      selectedIndex={state().selectedResultIndex} loading={state().searchLoading} error={state().error} />}>
      {(result) => <scrollbox flexGrow={1} minHeight={0}><text>{`Discovery: ${result.hits.length} paths${result.truncated ? " (truncated)" : ""}${result.error ? ` | Error: ${result.error}` : ""}`}</text>
        {result.hits.map((hit) => <text height={1} flexShrink={0}>{`${hit.path}${hit.line ? `:${hit.line}` : ""} ${hit.content ?? ""}`}</text>)}</scrollbox>}
    </Show>
  </box>;
}
