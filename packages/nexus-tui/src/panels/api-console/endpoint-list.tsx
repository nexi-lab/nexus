/**
 * Searchable, filterable list of all API endpoints.
 */

import React from "react";
import { useApiConsoleStore, type EndpointInfo } from "../../stores/api-console-store.js";

const METHOD_BADGE: Record<string, string> = {
  GET: "GET   ",
  POST: "POST  ",
  PUT: "PUT   ",
  DELETE: "DELETE",
  PATCH: "PATCH ",
  HEAD: "HEAD  ",
  OPTIONS: "OPT   ",
};

export function EndpointList(): React.ReactNode {
  const endpoints = useApiConsoleStore((s) => s.filteredEndpoints);
  const selectedEndpoint = useApiConsoleStore((s) => s.selectedEndpoint);
  const searchQuery = useApiConsoleStore((s) => s.searchQuery);

  if (endpoints.length === 0) {
    const message = searchQuery ? "No endpoints match your search" : "No endpoints loaded";
    return (
      <box height="100%" width="100%">
        <text>{message}</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      {endpoints.map((ep) => {
        const isSelected = selectedEndpoint?.path === ep.path && selectedEndpoint?.method === ep.method;
        return (
          <EndpointRow key={`${ep.method}:${ep.path}`} endpoint={ep} selected={isSelected} />
        );
      })}
    </scrollbox>
  );
}

function EndpointRow({
  endpoint,
  selected,
}: {
  endpoint: EndpointInfo;
  selected: boolean;
}): React.ReactNode {
  const prefix = selected ? "▸ " : "  ";
  const badge = METHOD_BADGE[endpoint.method] ?? endpoint.method;

  return (
    <box height={1} width="100%" flexDirection="row">
      <text>{`${prefix}${badge} ${endpoint.path}`}</text>
    </box>
  );
}
