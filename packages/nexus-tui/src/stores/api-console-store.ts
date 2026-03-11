/**
 * Zustand store for the API Console panel.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

/** Minimal OpenAPI 3.x spec shape — only what we parse. */
interface OpenApiSpec {
  readonly paths?: Readonly<Record<string, Record<string, unknown>>>;
}

// =============================================================================
// Types
// =============================================================================

export interface EndpointInfo {
  readonly method: string;
  readonly path: string;
  readonly summary: string;
  readonly tags: readonly string[];
}

export interface RequestState {
  readonly method: string;
  readonly path: string;
  readonly pathParams: Readonly<Record<string, string>>;
  readonly queryParams: Readonly<Record<string, string>>;
  readonly headers: Readonly<Record<string, string>>;
  readonly body: string;
}

export interface ResponseState {
  readonly status: number;
  readonly statusText: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly body: string;
  readonly timeMs: number;
  readonly error?: string;
}

export interface ConsoleHistoryEntry {
  readonly request: RequestState;
  readonly response: ResponseState;
  readonly timestamp: number;
}

const EMPTY_REQUEST: RequestState = {
  method: "GET",
  path: "",
  pathParams: {},
  queryParams: {},
  headers: {},
  body: "",
};

const MAX_HISTORY = 50;

// =============================================================================
// Store
// =============================================================================

export interface ApiConsoleState {
  // Endpoint registry
  readonly endpoints: readonly EndpointInfo[];
  readonly filteredEndpoints: readonly EndpointInfo[];
  readonly selectedEndpoint: EndpointInfo | null;
  readonly tagFilter: string | null;
  readonly searchQuery: string;

  // Request/response
  readonly request: RequestState;
  readonly response: ResponseState | null;
  readonly isLoading: boolean;

  // History
  readonly history: readonly ConsoleHistoryEntry[];

  // Actions
  readonly setEndpoints: (endpoints: readonly EndpointInfo[]) => void;
  readonly selectEndpoint: (ep: EndpointInfo) => void;
  readonly updateRequest: (partial: Partial<RequestState>) => void;
  readonly setTagFilter: (tag: string | null) => void;
  readonly setSearchQuery: (q: string) => void;
  readonly executeRequest: (client: FetchClient) => Promise<void>;
  readonly fetchOpenApiSpec: (client: FetchClient) => Promise<void>;
  readonly clearResponse: () => void;
}

export const useApiConsoleStore = create<ApiConsoleState>((set, get) => ({
  endpoints: [],
  filteredEndpoints: [],
  selectedEndpoint: null,
  tagFilter: null,
  searchQuery: "",
  request: EMPTY_REQUEST,
  response: null,
  isLoading: false,
  history: [],

  setEndpoints: (endpoints) => {
    set({ endpoints, filteredEndpoints: endpoints });
  },

  selectEndpoint: (ep) => {
    set({
      selectedEndpoint: ep,
      request: {
        method: ep.method,
        path: ep.path,
        pathParams: {},
        queryParams: {},
        headers: {},
        body: "",
      },
      response: null,
    });
  },

  updateRequest: (partial) => {
    set((state) => ({
      request: { ...state.request, ...partial },
    }));
  },

  setTagFilter: (tag) => {
    const { endpoints, searchQuery } = get();
    const filtered = filterEndpoints(endpoints, tag, searchQuery);
    set({ tagFilter: tag, filteredEndpoints: filtered });
  },

  setSearchQuery: (q) => {
    const { endpoints, tagFilter } = get();
    const filtered = filterEndpoints(endpoints, tagFilter, q);
    set({ searchQuery: q, filteredEndpoints: filtered });
  },

  executeRequest: async (client) => {
    const { request } = get();
    set({ isLoading: true, response: null });

    // Build the actual path with path params substituted
    let resolvedPath = request.path;
    for (const [key, value] of Object.entries(request.pathParams)) {
      resolvedPath = resolvedPath.replace(`{${key}}`, encodeURIComponent(value));
    }

    // Add query params
    const queryEntries = Object.entries(request.queryParams).filter(([, v]) => v !== "");
    if (queryEntries.length > 0) {
      const params = new URLSearchParams(queryEntries);
      resolvedPath += `?${params.toString()}`;
    }

    const start = performance.now();

    try {
      const fetchFn = (globalThis as Record<string, unknown>)["fetch"] as typeof globalThis.fetch;
      const baseUrl = (client as unknown as { baseUrl?: string })?.baseUrl ?? "http://localhost:2026";

      const headers: Record<string, string> = {
        Accept: "application/json",
        ...request.headers,
      };

      const init: RequestInit = {
        method: request.method,
        headers,
      };

      if (request.body && request.method !== "GET" && request.method !== "HEAD") {
        init.body = request.body;
        headers["Content-Type"] = "application/json";
      }

      const resp = await fetchFn(`${baseUrl}${resolvedPath}`, init);
      const timeMs = performance.now() - start;

      let body: string;
      const contentType = resp.headers.get("Content-Type") ?? "";
      if (contentType.includes("json")) {
        const json = await resp.json();
        body = JSON.stringify(json, null, 2);
      } else {
        body = await resp.text();
      }

      const responseHeaders: Record<string, string> = {};
      resp.headers.forEach((value, key) => {
        responseHeaders[key] = value;
      });

      const responseState: ResponseState = {
        status: resp.status,
        statusText: resp.statusText,
        headers: responseHeaders,
        body,
        timeMs,
      };

      const entry: ConsoleHistoryEntry = {
        request,
        response: responseState,
        timestamp: Date.now(),
      };

      set((state) => ({
        response: responseState,
        isLoading: false,
        history: [entry, ...state.history.slice(0, MAX_HISTORY - 1)],
      }));
    } catch (err) {
      const timeMs = performance.now() - start;
      const responseState: ResponseState = {
        status: 0,
        statusText: "Network Error",
        headers: {},
        body: "",
        timeMs,
        error: err instanceof Error ? err.message : "Request failed",
      };

      set({ response: responseState, isLoading: false });
    }
  },

  fetchOpenApiSpec: async (client) => {
    try {
      const spec = await client.get<OpenApiSpec>("/openapi.json");
      const endpoints: EndpointInfo[] = [];

      for (const [path, methods] of Object.entries(spec.paths ?? {})) {
        for (const [method, operation] of Object.entries(methods ?? {})) {
          if (method === "parameters" || typeof operation !== "object" || operation === null) continue;
          const op = operation as { summary?: string; tags?: string[] };
          endpoints.push({
            method: method.toUpperCase(),
            path,
            summary: op.summary ?? "",
            tags: op.tags ?? [],
          });
        }
      }

      // Sort: by path, then by method
      endpoints.sort((a, b) => a.path.localeCompare(b.path) || a.method.localeCompare(b.method));
      get().setEndpoints(endpoints);
    } catch {
      // OpenAPI spec not available — leave endpoints empty
    }
  },

  clearResponse: () => set({ response: null }),
}));

function filterEndpoints(
  endpoints: readonly EndpointInfo[],
  tag: string | null,
  query: string,
): readonly EndpointInfo[] {
  let result = endpoints;

  if (tag) {
    result = result.filter((ep) => ep.tags.includes(tag));
  }

  if (query) {
    const lower = query.toLowerCase();
    result = result.filter(
      (ep) =>
        ep.path.toLowerCase().includes(lower) ||
        ep.summary.toLowerCase().includes(lower) ||
        ep.method.toLowerCase().includes(lower),
    );
  }

  return result;
}
