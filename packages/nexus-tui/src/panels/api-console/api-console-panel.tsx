/**
 * API Console panel: endpoint list + request builder + response viewer.
 *
 * Press ":" to enter command input mode.
 * Supports CLI-like syntax (ls, cat, stat, rm, mkdir) and raw HTTP methods.
 * Arrow up/down navigates command history in input mode.
 */

import { createEffect } from "solid-js";
import type { JSX } from "solid-js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { useTextInput } from "../../shared/hooks/use-text-input.js";
import { useApiConsoleStore } from "../../stores/api-console-store.js";
import { EndpointList } from "./endpoint-list.js";
import { RequestBuilder } from "./request-builder.js";
import { ResponseViewer } from "./response-viewer.js";
import { CommandOutput } from "../../shared/components/command-output.js";
import { useCommandRunnerStore, executeLocalCommand } from "../../services/command-runner.js";
import { useUiStore } from "../../stores/ui-store.js";
import { focusColor } from "../../shared/theme.js";
import { Tooltip } from "../../shared/components/tooltip.js";

export default function ApiConsolePanel(): JSX.Element {
  const client = useApi();

  // Reactive state reads (wrapped in () => for SolidJS tracking)
  const commandRunnerStatus = () => useCommandRunnerStore((s) => s.status);
  const endpoints = () => useApiConsoleStore((s) => s.endpoints);
  const filteredEndpoints = () => useApiConsoleStore((s) => s.filteredEndpoints);
  const selectedEndpoint = () => useApiConsoleStore((s) => s.selectedEndpoint);
  const commandHistory = () => useApiConsoleStore((s) => s.commandHistory);
  const commandInputMode = () => useApiConsoleStore((s) => s.commandInputMode);
  const commandInputBuffer = () => useApiConsoleStore((s) => s.commandInputBuffer);
  const uiFocusPane = () => useUiStore((s) => s.getFocusPane("console"));
  const overlayActive = () => useUiStore((s) => s.overlayActive);

  // Stable action references (no () => needed)
  const selectEndpoint = useApiConsoleStore((s) => s.selectEndpoint);
  const executeRequest = useApiConsoleStore((s) => s.executeRequest);
  const executeCommand = useApiConsoleStore((s) => s.executeCommand);
  const fetchOpenApiSpec = useApiConsoleStore((s) => s.fetchOpenApiSpec);
  const setCommandInputMode = useApiConsoleStore((s) => s.setCommandInputMode);
  const setCommandInputBuffer = useApiConsoleStore((s) => s.setCommandInputBuffer);
  const navigateHistory = useApiConsoleStore((s) => s.navigateHistory);
  const setSearchQuery = useApiConsoleStore((s) => s.setSearchQuery);
  const toggleFocus = useUiStore((s) => s.toggleFocusPane);

  // Auto-load endpoints from OpenAPI spec on mount
  createEffect(() => {
    if (client && endpoints().length === 0) {
      fetchOpenApiSpec(client);
    }
  });

  // Endpoint filter input (replaces local endpointFilterMode/endpointFilter state)
  const endpointFilter = useTextInput({
    onSubmit: (val) => setSearchQuery(val.trim()),
    onCancel: () => { setSearchQuery(""); },
  });

  useKeyboard(
    (): Record<string, () => void> => {
      if (overlayActive()) return {};
      if (endpointFilter.active) return endpointFilter.inputBindings;

      const s = useApiConsoleStore.getState();
      if (s.commandInputMode) {
        return {
          return: () => {
            setCommandInputMode(false);
            const buf = useApiConsoleStore.getState().commandInputBuffer;
            if (client && buf.trim()) executeCommand(buf, client);
          },
          escape: () => setCommandInputMode(false),
          backspace: () => {
            const buf = useApiConsoleStore.getState().commandInputBuffer;
            setCommandInputBuffer(buf.slice(0, -1));
          },
          up: () => navigateHistory("up"),
          down: () => navigateHistory("down"),
        };
      }

      const fe = useApiConsoleStore.getState().filteredEndpoints;
      const sel = useApiConsoleStore.getState().selectedEndpoint;
      const selIdx = sel ? fe.findIndex((ep) => ep.path === sel.path && ep.method === sel.method) : -1;
      return {
        ...listNavigationBindings({
          getIndex: () => selIdx,
          setIndex: (i) => { const ep = fe[i]; if (ep) selectEndpoint(ep); },
          getLength: () => fe.length,
        }),
        return: () => { if (client) executeRequest(client); },
        "/": () => endpointFilter.activate(endpointFilter.buffer),
        ":": () => setCommandInputMode(true),
        "shift+b": () => { useCommandRunnerStore.getState().reset(); executeLocalCommand("build", []); },
        tab: () => toggleFocus("console"),
      };
    },
    (keyName: string) => {
      const s = useApiConsoleStore.getState();
      if (!s.commandInputMode) return;
      if (keyName.length === 1) {
        setCommandInputBuffer(s.commandInputBuffer + keyName);
      } else if (keyName === "space") {
        setCommandInputBuffer(s.commandInputBuffer + " ");
      }
    },
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <Tooltip tooltipKey="api-console-panel" message="Tip: Press ? for keybinding help" />
    <box flexGrow={1} width="100%" flexDirection="row">
      {/* Left: Endpoint list (30%) */}
      <box width="30%" height="100%" borderStyle="single" borderColor={uiFocusPane() === "left" ? focusColor.activeBorder : focusColor.inactiveBorder} flexDirection="column">
        <box height={1} width="100%">
          <text>{`─── Endpoints ─── (history: ${commandHistory().length})`}</text>
        </box>
        <box height={1} width="100%">
          <text>
            {endpointFilter.active
              ? `Filter: ${endpointFilter.buffer}\u2588`
              : endpointFilter.buffer
                ? `Filter: ${endpointFilter.buffer}  (Esc to clear)`
                : "/:filter endpoints"}
          </text>
        </box>
        <EndpointList />
      </box>

      {/* Right: Request + Response (70%) */}
      <box width="70%" height="100%" borderStyle="single" borderColor={uiFocusPane() === "right" ? focusColor.activeBorder : focusColor.inactiveBorder} flexDirection="column">
        {/* Command input bar */}
        <box height={1} width="100%">
          <text>
            {commandInputMode()
              ? `> ${commandInputBuffer()}█`
              : `Press ":" for command input | "!" prefix for local commands | Shift+B:build | history: ${commandHistory().length}`}
          </text>
        </box>

        {/* Local command output (when running via !command or Shift+B) */}
        {commandRunnerStatus() !== "idle" && (
          <box borderStyle="single" height={8} width="100%">
            <CommandOutput />
          </box>
        )}

        {/* Request builder (top 40%) */}
        <box flexGrow={4} borderStyle="single">
          <RequestBuilder />
        </box>

        {/* Response viewer (bottom 60%) */}
        <box flexGrow={6} borderStyle="single">
          <ResponseViewer />
        </box>
      </box>
    </box>
    </box>
  );
}
