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
const MAX_COMMAND_HISTORY = 100;

// =============================================================================
// CLI-like command parsing
// =============================================================================

export interface ParsedCommand {
  readonly method: string;
  readonly path: string;
  readonly body: string;
}

const CLI_COMMANDS: Readonly<
  Record<string, { readonly method: string; readonly pathFn: (arg: string) => string; readonly bodyFn?: (arg: string) => string }>
> = {
  ls: { method: "GET", pathFn: (p) => `/api/v2/files/list?path=${encodeURIComponent(p)}` },
  cat: { method: "GET", pathFn: (p) => `/api/v2/files/read?path=${encodeURIComponent(p)}` },
  stat: { method: "GET", pathFn: (p) => `/api/v2/files/metadata?path=${encodeURIComponent(p)}` },
  rm: { method: "DELETE", pathFn: (p) => `/api/v2/files?path=${encodeURIComponent(p)}` },
  mkdir: {
    method: "POST",
    pathFn: () => "/api/v2/files/mkdir",
    bodyFn: (p) => JSON.stringify({ path: p }),
  },
};

const HTTP_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]);

/**
 * Parse a CLI-like command string into method, path, and optional body.
 * Returns `null` if the input cannot be parsed.
 */
export function parseCommand(input: string): ParsedCommand | null {
  const trimmed = input.trim();
  if (!trimmed) return null;

  // Split on first space
  const spaceIdx = trimmed.indexOf(" ");
  if (spaceIdx === -1) return null;

  const firstWord = trimmed.slice(0, spaceIdx);
  const rest = trimmed.slice(spaceIdx + 1).trim();

  // CLI shorthand: ls, cat, stat, rm, mkdir
  const cmd = CLI_COMMANDS[firstWord];
  if (cmd) {
    return {
      method: cmd.method,
      path: cmd.pathFn(rest),
      body: cmd.bodyFn ? cmd.bodyFn(rest) : "",
    };
  }

  // Raw HTTP: METHOD /path [{body}]
  if (HTTP_METHODS.has(firstWord.toUpperCase())) {
    const method = firstWord.toUpperCase();
    // Check if rest has a JSON body after the path
    const bodyMatch = rest.match(/^(\S+)\s+(\{[\s\S]*\})$/);
    if (bodyMatch) {
      return { method, path: bodyMatch[1] ?? "", body: bodyMatch[2] ?? "" };
    }
    return { method, path: rest, body: "" };
  }

  return null;
}

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

  // Command history (for arrow-key navigation)
  readonly commandHistory: readonly string[];
  readonly historyIndex: number;

  // Command input mode
  readonly commandInputMode: boolean;
  readonly commandInputBuffer: string;

  // Actions
  readonly setEndpoints: (endpoints: readonly EndpointInfo[]) => void;
  readonly selectEndpoint: (ep: EndpointInfo) => void;
  readonly updateRequest: (partial: Partial<RequestState>) => void;
  readonly setTagFilter: (tag: string | null) => void;
  readonly setSearchQuery: (q: string) => void;
  readonly executeRequest: (client: FetchClient) => Promise<void>;
  readonly executeCommand: (input: string, client: FetchClient) => Promise<void>;
  readonly fetchOpenApiSpec: (client: FetchClient) => Promise<void>;
  readonly clearResponse: () => void;
  readonly navigateHistory: (direction: "up" | "down") => void;
  readonly setCommandInputMode: (enabled: boolean) => void;
  readonly setCommandInputBuffer: (buffer: string) => void;
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
  commandHistory: [],
  historyIndex: -1,
  commandInputMode: false,
  commandInputBuffer: "",

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

      const commandStr = `${request.method} ${request.path}`;
      set((state) => ({
        response: responseState,
        isLoading: false,
        history: [entry, ...state.history.slice(0, MAX_HISTORY - 1)],
        commandHistory: [
          ...state.commandHistory,
          commandStr,
        ].slice(-MAX_COMMAND_HISTORY),
        historyIndex: -1,
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

  executeCommand: async (input, client) => {
    const parsed = parseCommand(input);
    if (!parsed) return;

    get().updateRequest({
      method: parsed.method,
      path: parsed.path,
      body: parsed.body,
      pathParams: {},
      queryParams: {},
      headers: {},
    });

    // Wait one tick so state is flushed before executing
    await Promise.resolve();
    await get().executeRequest(client);
  },

  navigateHistory: (direction) => {
    const { commandHistory, historyIndex } = get();
    if (commandHistory.length === 0) return;

    let newIndex: number;
    if (direction === "up") {
      // Move backward through history (toward older commands)
      if (historyIndex === -1) {
        newIndex = commandHistory.length - 1;
      } else {
        newIndex = Math.max(historyIndex - 1, 0);
      }
    } else {
      // Move forward through history (toward newer commands)
      if (historyIndex === -1) return;
      newIndex = historyIndex + 1;
      if (newIndex >= commandHistory.length) {
        // Past the newest entry — clear
        set({ historyIndex: -1, commandInputBuffer: "" });
        return;
      }
    }

    const entry = commandHistory[newIndex];
    set({
      historyIndex: newIndex,
      commandInputBuffer: entry ?? "",
    });
  },

  setCommandInputMode: (enabled) => {
    set({ commandInputMode: enabled, historyIndex: -1 });
    if (enabled) {
      set({ commandInputBuffer: "" });
    }
  },

  setCommandInputBuffer: (buffer) => {
    set({ commandInputBuffer: buffer });
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
