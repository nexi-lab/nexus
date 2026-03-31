/**
 * API Console panel: endpoint list + request builder + response viewer.
 *
 * Press ":" to enter command input mode.
 * Supports CLI-like syntax (ls, cat, stat, rm, mkdir) and raw HTTP methods.
 * Arrow up/down navigates command history in input mode.
 */

import React, { useEffect, useCallback } from "react";
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

export default function ApiConsolePanel(): React.ReactNode {
  const client = useApi();
  // Reactive subscription to command runner status (Codex finding 2)
  const commandRunnerStatus = useCommandRunnerStore((s) => s.status);
  const endpoints = useApiConsoleStore((s) => s.endpoints);
  const filteredEndpoints = useApiConsoleStore((s) => s.filteredEndpoints);
  const selectedEndpoint = useApiConsoleStore((s) => s.selectedEndpoint);
  const selectEndpoint = useApiConsoleStore((s) => s.selectEndpoint);
  const executeRequest = useApiConsoleStore((s) => s.executeRequest);
  const executeCommand = useApiConsoleStore((s) => s.executeCommand);
  const fetchOpenApiSpec = useApiConsoleStore((s) => s.fetchOpenApiSpec);
  const commandHistory = useApiConsoleStore((s) => s.commandHistory);
  const commandInputMode = useApiConsoleStore((s) => s.commandInputMode);
  const commandInputBuffer = useApiConsoleStore((s) => s.commandInputBuffer);
  const setCommandInputMode = useApiConsoleStore((s) => s.setCommandInputMode);
  const setCommandInputBuffer = useApiConsoleStore((s) => s.setCommandInputBuffer);
  const navigateHistory = useApiConsoleStore((s) => s.navigateHistory);

  const setSearchQuery = useApiConsoleStore((s) => s.setSearchQuery);

  // Focus pane (ui-store)
  const uiFocusPane = useUiStore((s) => s.getFocusPane("console"));
  const toggleFocus = useUiStore((s) => s.toggleFocusPane);
  const overlayActive = useUiStore((s) => s.overlayActive);

  // Auto-load endpoints from OpenAPI spec on mount
  useEffect(() => {
    if (client && endpoints.length === 0) {
      fetchOpenApiSpec(client);
    }
  }, [client, endpoints.length, fetchOpenApiSpec]);

  // Find current selection index
  const selectedIdx = selectedEndpoint
    ? filteredEndpoints.findIndex((ep) => ep.path === selectedEndpoint.path && ep.method === selectedEndpoint.method)
    : -1;

  // Shared list navigation (j/k/up/down/g/G)
  const listNav = listNavigationBindings({
    getIndex: () => selectedIdx,
    setIndex: (i) => {
      const ep = filteredEndpoints[i];
      if (ep) selectEndpoint(ep);
    },
    getLength: () => filteredEndpoints.length,
  });

  // Endpoint filter input (replaces local endpointFilterMode/endpointFilter state)
  const endpointFilter = useTextInput({
    onSubmit: (val) => setSearchQuery(val.trim()),
    onCancel: () => { setSearchQuery(""); },
  });

  // Handle printable characters in command input mode
  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      if (!commandInputMode) return;
      if (keyName.length === 1) {
        setCommandInputBuffer(commandInputBuffer + keyName);
      } else if (keyName === "space") {
        setCommandInputBuffer(commandInputBuffer + " ");
      }
    },
    [commandInputMode, commandInputBuffer, setCommandInputBuffer],
  );

  useKeyboard(
    overlayActive
      ? {}
      : endpointFilter.active
      ? endpointFilter.inputBindings
      : commandInputMode
      ? {
          return: () => {
            setCommandInputMode(false);
            if (client && commandInputBuffer.trim()) {
              executeCommand(commandInputBuffer, client);
            }
          },
          escape: () => {
            setCommandInputMode(false);
          },
          backspace: () => {
            setCommandInputBuffer(commandInputBuffer.slice(0, -1));
          },
          up: () => navigateHistory("up"),
          down: () => navigateHistory("down"),
        }
      : {
          ...listNav,
          return: () => {
            if (client) executeRequest(client);
          },
          "/": () => {
            endpointFilter.activate(endpointFilter.buffer);
          },
          ":": () => {
            setCommandInputMode(true);
          },
          // Issue #3078: Shift+B to run nexus build from Console
          "shift+b": () => {
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("build", []);
          },
          tab: () => toggleFocus("console"),
        },
    overlayActive
      ? undefined
      : endpointFilter.active
      ? endpointFilter.onUnhandled
      : handleUnhandledKey,
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <Tooltip tooltipKey="api-console-panel" message="Tip: Press ? for keybinding help" />
    <box flexGrow={1} width="100%" flexDirection="row">
      {/* Left: Endpoint list (30%) */}
      <box width="30%" height="100%" borderStyle="single" borderColor={uiFocusPane === "left" ? focusColor.activeBorder : focusColor.inactiveBorder} flexDirection="column">
        <box height={1} width="100%">
          <text>{`─── Endpoints ─── (history: ${commandHistory.length})`}</text>
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
      <box width="70%" height="100%" borderStyle="single" borderColor={uiFocusPane === "right" ? focusColor.activeBorder : focusColor.inactiveBorder} flexDirection="column">
        {/* Command input bar */}
        <box height={1} width="100%">
          <text>
            {commandInputMode
              ? `> ${commandInputBuffer}█`
              : `Press ":" for command input | "!" prefix for local commands | Shift+B:build | history: ${commandHistory.length}`}
          </text>
        </box>

        {/* Local command output (when running via !command or Shift+B) */}
        {commandRunnerStatus !== "idle" && (
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
