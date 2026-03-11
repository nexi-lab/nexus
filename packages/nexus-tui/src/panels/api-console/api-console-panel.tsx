/**
 * API Console panel: endpoint list + request builder + response viewer.
 */

import React, { useEffect } from "react";
import { useApi } from "../../shared/hooks/use-api.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApiConsoleStore } from "../../stores/api-console-store.js";
import { EndpointList } from "./endpoint-list.js";
import { RequestBuilder } from "./request-builder.js";
import { ResponseViewer } from "./response-viewer.js";

export default function ApiConsolePanel(): React.ReactNode {
  const client = useApi();
  const endpoints = useApiConsoleStore((s) => s.endpoints);
  const filteredEndpoints = useApiConsoleStore((s) => s.filteredEndpoints);
  const selectedEndpoint = useApiConsoleStore((s) => s.selectedEndpoint);
  const selectEndpoint = useApiConsoleStore((s) => s.selectEndpoint);
  const executeRequest = useApiConsoleStore((s) => s.executeRequest);
  const fetchOpenApiSpec = useApiConsoleStore((s) => s.fetchOpenApiSpec);

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

  useKeyboard({
    "j": () => {
      const next = Math.min(selectedIdx + 1, filteredEndpoints.length - 1);
      const ep = filteredEndpoints[next];
      if (ep) selectEndpoint(ep);
    },
    "down": () => {
      const next = Math.min(selectedIdx + 1, filteredEndpoints.length - 1);
      const ep = filteredEndpoints[next];
      if (ep) selectEndpoint(ep);
    },
    "k": () => {
      const prev = Math.max(selectedIdx - 1, 0);
      const ep = filteredEndpoints[prev];
      if (ep) selectEndpoint(ep);
    },
    "up": () => {
      const prev = Math.max(selectedIdx - 1, 0);
      const ep = filteredEndpoints[prev];
      if (ep) selectEndpoint(ep);
    },
    "return": () => {
      if (client) executeRequest(client);
    },
  });

  return (
    <box height="100%" width="100%" flexDirection="row">
      {/* Left: Endpoint list (30%) */}
      <box width="30%" height="100%" borderStyle="single">
        <box height={1} width="100%">
          <text>{"─── Endpoints ───"}</text>
        </box>
        <EndpointList />
      </box>

      {/* Right: Request + Response (70%) */}
      <box width="70%" height="100%" flexDirection="column">
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
  );
}
