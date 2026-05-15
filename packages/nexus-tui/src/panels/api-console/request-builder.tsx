import type { JSX } from "solid-js";
/**
 * Dynamic form for building API requests.
 */

import { useApiConsoleStore } from "../../stores/api-console-store.js";
import { Spinner } from "../../shared/components/spinner.js";

export function RequestBuilder(): JSX.Element {
  const request = useApiConsoleStore((s) => s.request);
  const selectedEndpoint = useApiConsoleStore((s) => s.selectedEndpoint);
  const isLoading = useApiConsoleStore((s) => s.isLoading);

  if (!selectedEndpoint) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Select an endpoint from the list</text>
      </box>
    );
  }

  // Extract path parameters (e.g., {id}, {name})
  const pathParamNames = [...request.path.matchAll(/\{(\w+)\}/g)].map((m) => m[1]!);

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Method + Path */}
      <box height={1} width="100%">
        <text>{`${request.method} ${request.path}`}</text>
      </box>

      {/* Summary */}
      {selectedEndpoint.summary && (
        <box height={1} width="100%">
          <text>{selectedEndpoint.summary}</text>
        </box>
      )}

      {/* Path Parameters */}
      {pathParamNames.length > 0 && (
        <box flexDirection="column">
          <text>{"─── Path Parameters ───"}</text>
          {pathParamNames.map((name) => (
            <box height={1} width="100%">
              <text>{`  ${name}: ${request.pathParams[name] ?? ""}`}</text>
            </box>
          ))}
        </box>
      )}

      {/* Request Body */}
      {request.method !== "GET" && request.method !== "HEAD" && (
        <box flexDirection="column" flexGrow={1}>
          <text>{"─── Request Body (JSON) ───"}</text>
          <text>{request.body || "{}"}</text>
        </box>
      )}

      {/* Send button area */}
      <box height={1} width="100%">
        {isLoading ? (
          <Spinner label="Sending..." />
        ) : (
          <text>{"Enter to send request"}</text>
        )}
      </box>
    </box>
  );
}
