import { createSignal, Show, Switch, Match, onCleanup } from "solid-js";
import { useRenderer } from "@opentui/solid";
import { useGlobalStore, type PanelId } from "./stores/global-store.js";
import { useSearchStore } from "./stores/search-store.js";
import { useKeyboard } from "./shared/hooks/use-keyboard.js";
import { StatusBar } from "./shared/components/status-bar.js";
import { TerminalGuard } from "./shared/components/terminal-guard.js";
import { resetTerminal } from "./utils/terminal.js";
import FileExplorerPanel from "./panels/files/file-explorer-panel.js";
import SearchPanel from "./panels/search/search-panel.js";
import ApiConsolePanel from "./panels/api-console/api-console-panel.js";

function Workspace(props: { readonly inputMode: (active: boolean) => void }) {
  onCleanup(() => { useSearchStore.getState().reset(); props.inputMode(false); });
  return <Switch>
    <Match when={useGlobalStore.getState().activePanel === "files"}><FileExplorerPanel onInputMode={props.inputMode} /></Match>
    <Match when={useGlobalStore.getState().activePanel === "search"}><SearchPanel onInputMode={props.inputMode} /></Match>
    <Match when={useGlobalStore.getState().activePanel === "console"}><ApiConsolePanel onInputMode={props.inputMode} /></Match>
  </Switch>;
}
export function App() {
  const renderer = useRenderer();
  const [inputMode, setInputMode] = createSignal(false);
  const state = useGlobalStore.getState;
  const navigate = (panel: PanelId) => { if (!inputMode()) state().setActivePanel(panel); };
  useKeyboard(() => inputMode() ? {} : {
    "1": () => navigate("files"), "2": () => navigate("search"), "3": () => navigate("console"),
    r: state().connectionStatus === "connected" ? undefined : () => { void state().testConnection(); },
    q: () => { renderer.destroy(); resetTerminal(); process.exit(0); },
  });
  return <TerminalGuard><box height="100%" width="100%" flexDirection="column">
    <text height={1} flexShrink={0}>{"Nexus native | 1:Files  2:Search  3:Console  q:quit"}</text>
    <Show when={state().connectionStatus === "connected"} fallback={
      <box flexGrow={1} flexDirection="column"><text height={1} flexShrink={0}>{state().connectionError ?? "Connect using NEXUS_URL and NEXUS_API_KEY, then press r to retry."}</text>
        <text height={1} flexShrink={0}>{"Native credentials are verified by /v2/auth/whoami."}</text></box>
    }><box flexGrow={1} minHeight={0} width="100%"><Workspace inputMode={setInputMode} /></box></Show>
    <StatusBar />
  </box></TerminalGuard>;
}
