import { describe, it, expect, beforeEach } from "bun:test";
import { useApiConsoleStore, type EndpointInfo } from "../../src/stores/api-console-store.js";

const SAMPLE_ENDPOINTS: readonly EndpointInfo[] = [
  { method: "GET", path: "/api/v2/files/list", summary: "List files", tags: ["files"] },
  { method: "POST", path: "/api/v2/files/write", summary: "Write file", tags: ["files"] },
  { method: "GET", path: "/api/v2/pay/balance", summary: "Get balance", tags: ["payments"] },
  { method: "POST", path: "/api/v2/agents/{id}/evict", summary: "Evict agent", tags: ["agents"] },
];

describe("ApiConsoleStore", () => {
  beforeEach(() => {
    useApiConsoleStore.setState({
      endpoints: [],
      filteredEndpoints: [],
      selectedEndpoint: null,
      tagFilter: null,
      searchQuery: "",
      request: { method: "GET", path: "", pathParams: {}, queryParams: {}, headers: {}, body: "" },
      response: null,
      isLoading: false,
      history: [],
    });
  });

  describe("setEndpoints", () => {
    it("stores and sets filtered to all", () => {
      useApiConsoleStore.getState().setEndpoints(SAMPLE_ENDPOINTS);
      const state = useApiConsoleStore.getState();
      expect(state.endpoints.length).toBe(4);
      expect(state.filteredEndpoints.length).toBe(4);
    });
  });

  describe("selectEndpoint", () => {
    it("sets selected and initializes request", () => {
      useApiConsoleStore.getState().selectEndpoint(SAMPLE_ENDPOINTS[3]!);
      const state = useApiConsoleStore.getState();
      expect(state.selectedEndpoint?.method).toBe("POST");
      expect(state.request.method).toBe("POST");
      expect(state.request.path).toBe("/api/v2/agents/{id}/evict");
      expect(state.response).toBeNull();
    });
  });

  describe("updateRequest", () => {
    it("merges partial into request", () => {
      useApiConsoleStore.getState().selectEndpoint(SAMPLE_ENDPOINTS[0]!);
      useApiConsoleStore.getState().updateRequest({ body: '{"test": true}' });
      expect(useApiConsoleStore.getState().request.body).toBe('{"test": true}');
      expect(useApiConsoleStore.getState().request.method).toBe("GET"); // preserved
    });
  });

  describe("setTagFilter", () => {
    it("filters endpoints by tag", () => {
      useApiConsoleStore.getState().setEndpoints(SAMPLE_ENDPOINTS);
      useApiConsoleStore.getState().setTagFilter("payments");
      const filtered = useApiConsoleStore.getState().filteredEndpoints;
      expect(filtered.length).toBe(1);
      expect(filtered[0]!.summary).toBe("Get balance");
    });

    it("null tag shows all", () => {
      useApiConsoleStore.getState().setEndpoints(SAMPLE_ENDPOINTS);
      useApiConsoleStore.getState().setTagFilter("payments");
      useApiConsoleStore.getState().setTagFilter(null);
      expect(useApiConsoleStore.getState().filteredEndpoints.length).toBe(4);
    });
  });

  describe("setSearchQuery", () => {
    it("filters by path", () => {
      useApiConsoleStore.getState().setEndpoints(SAMPLE_ENDPOINTS);
      useApiConsoleStore.getState().setSearchQuery("files");
      const filtered = useApiConsoleStore.getState().filteredEndpoints;
      expect(filtered.length).toBe(2);
    });

    it("filters by method", () => {
      useApiConsoleStore.getState().setEndpoints(SAMPLE_ENDPOINTS);
      useApiConsoleStore.getState().setSearchQuery("POST");
      const filtered = useApiConsoleStore.getState().filteredEndpoints;
      expect(filtered.length).toBe(2);
    });

    it("combines with tag filter", () => {
      useApiConsoleStore.getState().setEndpoints(SAMPLE_ENDPOINTS);
      useApiConsoleStore.getState().setTagFilter("files");
      useApiConsoleStore.getState().setSearchQuery("POST");
      const filtered = useApiConsoleStore.getState().filteredEndpoints;
      expect(filtered.length).toBe(1);
      expect(filtered[0]!.path).toBe("/api/v2/files/write");
    });
  });

  describe("clearResponse", () => {
    it("clears response state", () => {
      useApiConsoleStore.setState({
        response: { status: 200, statusText: "OK", headers: {}, body: "{}", timeMs: 50 },
      });
      useApiConsoleStore.getState().clearResponse();
      expect(useApiConsoleStore.getState().response).toBeNull();
    });
  });
});
