import { createSignal, Show, onCleanup } from "solid-js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useRequestScope } from "../../shared/hooks/use-request-scope.js";
import { NativePrompt } from "../../shared/components/native-prompt.js";
import { parseNativeRequest } from "./native-request.js";

export default function ApiConsolePanel(props: { readonly onInputMode: (active: boolean) => void }) {
  const [editing, setEditing] = createSignal(false);
  const [request, setRequest] = createSignal("GET /v2/auth/whoami");
  const [result, setResult] = createSignal("");
  const scope = useRequestScope();
  onCleanup(() => props.onInputMode(false));
  const close = () => { setEditing(false); props.onInputMode(false); };
  const send = (text: string) => {
    close(); setRequest(text); setResult("");
    void scope.run(async (signal) => {
      const parsed = parseNativeRequest(text);
      const response = await useGlobalStore.getState().client!.rawRequest(parsed.method, parsed.path, parsed.body, {
        signal, headers: parsed.body === undefined ? {} : { "Content-Type": parsed.contentType },
      });
      const bytes = new Uint8Array(await response.arrayBuffer());
      let body: string;
      try { body = new TextDecoder("utf-8", { fatal: true }).decode(bytes); } catch { body = `Binary response: ${bytes.length} bytes`; }
      return `${response.status} ${response.statusText}\n${body.slice(0, 65536)}${body.length > 65536 ? "\n[preview truncated]" : ""}`;
    }, setResult);
  };
  useKeyboard(() => editing() ? {} : { i: () => { props.onInputMode(true); setEditing(true); } });
  return <box height="100%" width="100%" flexDirection="column">
    <text height={1} flexShrink={0}>{"Native Console | i:request | METHOD /v2/path [JSON body]"}</text>
    <text height={1} flexShrink={0}>{"Credentials: GET /v2/auth/keys | Relationships: GET /v2/rebac/tuples?zone=root"}</text>
    <Show when={editing()}><NativePrompt label="Native HTTP request" initialValue={request()} onSubmit={send} onCancel={close} /></Show>
    <Show when={scope.pending()}><text height={1} flexShrink={0}>{"Request in progress…"}</text></Show>
    <Show when={scope.error()}><text height={1} flexShrink={0}>{`Error: ${scope.error()}`}</text></Show>
    <scrollbox flexGrow={1} minHeight={0}><text>{result()}</text></scrollbox>
  </box>;
}
