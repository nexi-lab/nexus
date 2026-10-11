import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useSearchStore } from "../../src/stores/search-store.js";
import type { FetchClient } from "@nexus-ai-fs/api-client";

function mockClient(responses: Record<string, unknown>): FetchClient {
  return {
    get: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked path: ${path}`);
    }),
    post: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked path: ${path}`);
    }),
    delete: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked path: ${path}`);
    }),
  } as unknown as FetchClient;
}

function resetStore(): void {
  useSearchStore.setState({
    searchQuery: "",
    searchMode: "hybrid",
    searchResults: [],
    searchTotal: 0,
    selectedResultIndex: 0,
    searchLoading: false,
    memories: [],
    selectedMemoryIndex: 0,
    memoriesLoading: false,
    memoryHistory: null,
    memoryHistoryLoading: false,
    memoryDiff: null,
    memoryDiffLoading: false,
    playbooks: [],
    playbooksLoading: false,
    selectedPlaybookIndex: 0,
    rlmAnswer: null,
    rlmLoading: false,
    rlmContextPaths: [],
    activeTab: "search",
    error: null,
  });
}

describe("SearchStore", () => {
  beforeEach(() => {
    resetStore();
  });

  describe("setActiveTab", () => {
    it("switches between tabs and clears error", () => {
      useSearchStore.setState({ error: "old error" });
      useSearchStore.getState().setActiveTab("memories");
      expect(useSearchStore.getState().activeTab).toBe("memories");
      expect(useSearchStore.getState().error).toBeNull();
    });

    it("cycles through all tabs", () => {
      useSearchStore.getState().setActiveTab("memories");
      expect(useSearchStore.getState().activeTab).toBe("memories");

      useSearchStore.getState().setActiveTab("search");
      expect(useSearchStore.getState().activeTab).toBe("search");
    });
  });

  describe("setSearchQuery", () => {
    it("updates the search query", () => {
      useSearchStore.getState().setSearchQuery("test query");
      expect(useSearchStore.getState().searchQuery).toBe("test query");
    });

    it("clears the search query", () => {
      useSearchStore.setState({ searchQuery: "existing" });
      useSearchStore.getState().setSearchQuery("");
      expect(useSearchStore.getState().searchQuery).toBe("");
    });
  });

  describe("setSelectedResultIndex", () => {
    it("sets the selected result index", () => {
      useSearchStore.getState().setSelectedResultIndex(5);
      expect(useSearchStore.getState().selectedResultIndex).toBe(5);
    });
  });

  describe("setSelectedMemoryIndex", () => {
    it("sets the selected memory index", () => {
      useSearchStore.getState().setSelectedMemoryIndex(3);
      expect(useSearchStore.getState().selectedMemoryIndex).toBe(3);
    });
  });

  describe("search", () => {
    it("uses the Rust envelope and preserves chunk attribution without line fields", async () => {
      const hit = { path: "/docs/test.txt", zone_id: "sharedzone", chunk_text: "native chunk", chunk_index: 0, score: 0.95, keyword_score: 0.8 };
      const client = mockClient({ "/v2/search/query": { results: [hit] } });
      await useSearchStore.getState().search("test & more", client, { url: "http://search", zoneId: "sharedzone" });
      expect(client.get).not.toHaveBeenCalled();
      expect(client.post).toHaveBeenCalledWith("/v2/search/query", {
        q: "test & more", query_type: "hybrid", path_filter: "/", limit: 10, zone_id: "sharedzone",
      }, { transformKeys: false, baseUrl: "http://search" });
      const state = useSearchStore.getState();
      expect(state.searchResults).toEqual([hit]);
      expect(state.searchTotal).toBe(1);
      expect(state.searchLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("uses the selected search mode and leaves unpinned zone discovery to the host", async () => {
      useSearchStore.setState({ searchMode: "keyword" });
      const client = mockClient({ "/v2/search/query": { results: [] } });
      await useSearchStore.getState().search("test", client);
      expect(client.post).toHaveBeenCalledWith("/v2/search/query", {
        q: "test", query_type: "keyword", path_filter: "/", limit: 10, zone_id: "",
      }, { transformKeys: false });
    });

    it("retains partial hits and reports the host's failed-zone error", async () => {
      const hit = { path: "/docs/test.txt", zone_id: "sharedzone", chunk_text: "native chunk", chunk_index: 0, score: 0.95 };
      const client = mockClient({ "/v2/search/query": { results: [hit], error: "1 zone(s) failed" } });
      await useSearchStore.getState().search("test", client);
      expect(useSearchStore.getState().searchResults).toEqual([hit]);
      expect(useSearchStore.getState().error).toBe("1 zone(s) failed");
    });

    it("clears stale hits and surfaces transport failures", async () => {
      useSearchStore.setState({ searchResults: [{ path: "/old", zone_id: "old", chunk_text: "old", chunk_index: 0, score: 1 }], searchTotal: 1 });
      const client = { post: mock(async () => { throw new Error("Search host unavailable"); }) } as unknown as FetchClient;
      await useSearchStore.getState().search("test", client);
      expect(useSearchStore.getState().searchResults).toEqual([]);
      expect(useSearchStore.getState().searchTotal).toBe(0);
      expect(useSearchStore.getState().searchLoading).toBe(false);
      expect(useSearchStore.getState().error).toBe("Search host unavailable");
    });
  });

  describe("fetchMemories", () => {
    it("uses POST to /api/v2/memories/search with query body", async () => {
      const client = mockClient({
        "/api/v2/memories/search": {
          memories: [
            {
              memory_id: "mem-1",
              agent_id: "agent-1",
              type: "episodic",
              content: "User asked about deployment steps",
            },
            {
              memory_id: "mem-2",
              agent_id: "agent-2",
              type: "semantic",
              content: "API rate limits are 100 req/s",
            },
          ],
        },
      });

      await useSearchStore.getState().fetchMemories("deployment", client);
      const state = useSearchStore.getState();

      // Verify POST was called (not GET)
      expect(client.post).toHaveBeenCalled();
      expect(client.get).not.toHaveBeenCalled();

      expect(state.memories).toHaveLength(2);
      expect((state.memories[0] as Record<string, unknown>).memory_id).toBe("mem-1");
      expect((state.memories[0] as Record<string, unknown>).type).toBe("episodic");
      expect((state.memories[1] as Record<string, unknown>).content).toBe("API rate limits are 100 req/s");
      expect(state.selectedMemoryIndex).toBe(0);
      expect(state.memoriesLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Memory service down"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().fetchMemories("test", client);
      const state = useSearchStore.getState();
      expect(state.memoriesLoading).toBe(false);
      expect(state.error).toBe("Memory service down");
    });
  });

  describe("fetchMemoryDetail", () => {
    it("fetches from /api/v2/memories/{id} (plural) and unwraps memory field", async () => {
      useSearchStore.setState({
        memories: [
          {
            memory_id: "mem-1",
            agent_id: "agent-1",
            type: "episodic",
            content: "short",
          },
        ],
      });

      const client = mockClient({
        "/api/v2/memories/mem-1": {
          memory: {
            memory_id: "mem-1",
            agent_id: "agent-1",
            type: "episodic",
            content: "Full detailed content of the memory",
            tags: ["updated"],
            version: 2,
          },
        },
      });

      await useSearchStore.getState().fetchMemoryDetail("mem-1", client);
      const state = useSearchStore.getState();

      expect((state.memories[0] as Record<string, unknown>).content).toBe("Full detailed content of the memory");
      expect((state.memories[0] as Record<string, unknown>).version).toBe(2);
      expect((state.memories[0] as Record<string, unknown>).tags).toEqual(["updated"]);
      expect(state.memoriesLoading).toBe(false);
    });

    it("uses GET to /api/v2/memories/ (plural path)", async () => {
      useSearchStore.setState({ memories: [] });

      const client = mockClient({
        "/api/v2/memories/some-id": {
          memory: { memory_id: "some-id", content: "detail" },
        },
      });

      await useSearchStore.getState().fetchMemoryDetail("some-id", client);

      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("/api/v2/memories/");
      expect(calledUrl).not.toContain("/api/v2/memory/");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Memory not found"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().fetchMemoryDetail("missing", client);
      const state = useSearchStore.getState();
      expect(state.memoriesLoading).toBe(false);
      expect(state.error).toBe("Memory not found");
    });
  });

  describe("fetchPlaybooks", () => {
    it("uses GET on /api/v2/playbooks with name_pattern param and stores results", async () => {
      const client = mockClient({
        "/api/v2/playbooks": {
          playbooks: [
            {
              playbook_id: "pb-1",
              name: "Deploy Service",
              description: "Steps to deploy a microservice",
              version: 2,
              scope: "team",
              visibility: "public",
              usage_count: 42,
              success_rate: 0.95,
              strategies: [{ type: "sequential" }],
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-06-15T12:00:00Z",
            },
            {
              playbook_id: "pb-2",
              name: "Onboard Agent",
              description: null,
              version: 1,
              scope: "global",
              visibility: "private",
              usage_count: 7,
              success_rate: null,
              strategies: null,
              created_at: "2025-02-01T00:00:00Z",
              updated_at: null,
            },
          ],
          total: 2,
        },
      });

      await useSearchStore.getState().fetchPlaybooks("deploy", client);
      const state = useSearchStore.getState();

      expect(client.get).toHaveBeenCalled();
      expect(state.playbooks).toHaveLength(2);
      expect(state.playbooks[0]!.playbook_id).toBe("pb-1");
      expect(state.playbooks[0]!.name).toBe("Deploy Service");
      expect(state.playbooks[0]!.scope).toBe("team");
      expect(state.playbooks[0]!.visibility).toBe("public");
      expect(state.playbooks[0]!.version).toBe(2);
      expect(state.playbooks[0]!.usage_count).toBe(42);
      expect(state.playbooks[0]!.success_rate).toBe(0.95);
      expect(state.playbooks[1]!.playbook_id).toBe("pb-2");
      expect(state.playbooks[1]!.success_rate).toBeNull();
      expect(state.playbooks[1]!.description).toBeNull();
      expect(state.selectedPlaybookIndex).toBe(0);
      expect(state.playbooksLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("includes name_pattern in GET url", async () => {
      const client = mockClient({
        "/api/v2/playbooks": { playbooks: [], total: 0 },
      });

      await useSearchStore.getState().fetchPlaybooks("test-pattern", client);

      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("name_pattern=test-pattern");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Playbook service unavailable"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().fetchPlaybooks("fail", client);
      const state = useSearchStore.getState();
      expect(state.playbooksLoading).toBe(false);
      expect(state.error).toBe("Playbook service unavailable");
    });
  });

  describe("deletePlaybook", () => {
    it("calls DELETE on /api/v2/playbooks/{id} and removes from local list", async () => {
      useSearchStore.setState({
        playbooks: [
          {
            playbook_id: "pb-1",
            name: "Deploy Service",
            description: "desc",
            version: 1,
            scope: "team",
            visibility: "public",
            usage_count: 5,
            success_rate: 0.9,
            strategies: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          },
          {
            playbook_id: "pb-2",
            name: "Onboard Agent",
            description: null,
            version: 1,
            scope: "global",
            visibility: "private",
            usage_count: 3,
            success_rate: null,
            strategies: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: null,
          },
        ],
        selectedPlaybookIndex: 0,
      });

      const client = mockClient({
        "/api/v2/playbooks/pb-1": {},
      });

      await useSearchStore.getState().deletePlaybook("pb-1", client);
      const state = useSearchStore.getState();

      expect((client.delete as ReturnType<typeof mock>).mock.calls.length).toBe(1);
      expect(state.playbooks).toHaveLength(1);
      expect(state.playbooks[0]!.playbook_id).toBe("pb-2");
      expect(state.error).toBeNull();
    });

    it("adjusts selectedPlaybookIndex when deleting last item", async () => {
      useSearchStore.setState({
        playbooks: [
          {
            playbook_id: "pb-1",
            name: "Only Playbook",
            description: "desc",
            version: 1,
            scope: "team",
            visibility: "public",
            usage_count: 1,
            success_rate: 1.0,
            strategies: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          },
        ],
        selectedPlaybookIndex: 0,
      });

      const client = mockClient({
        "/api/v2/playbooks/pb-1": {},
      });

      await useSearchStore.getState().deletePlaybook("pb-1", client);
      const state = useSearchStore.getState();

      expect(state.playbooks).toHaveLength(0);
      expect(state.selectedPlaybookIndex).toBe(0);
    });

    it("sets error on failure", async () => {
      const client = {
        delete: mock(async () => { throw new Error("Forbidden"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().deletePlaybook("pb-1", client);
      const state = useSearchStore.getState();
      expect(state.error).toBe("Forbidden");
    });
  });

  describe("setSelectedPlaybookIndex", () => {
    it("sets the selected playbook index", () => {
      useSearchStore.getState().setSelectedPlaybookIndex(4);
      expect(useSearchStore.getState().selectedPlaybookIndex).toBe(4);
    });

    it("sets to zero", () => {
      useSearchStore.setState({ selectedPlaybookIndex: 3 });
      useSearchStore.getState().setSelectedPlaybookIndex(0);
      expect(useSearchStore.getState().selectedPlaybookIndex).toBe(0);
    });
  });

  describe("fetchMemoryHistory", () => {
    it("fetches version history from /api/v2/memories/{id}/history", async () => {
      const client = mockClient({
        "/api/v2/memories/mem-1/history": {
          memory_id: "mem-1",
          current_version: 3,
          versions: [
            { version: 1, created_at: "2025-01-01T00:00:00Z", status: "superseded" },
            { version: 2, created_at: "2025-02-01T00:00:00Z", status: "superseded" },
            { version: 3, created_at: "2025-03-01T00:00:00Z", status: "active" },
          ],
        },
      });

      await useSearchStore.getState().fetchMemoryHistory("mem-1", client);
      const state = useSearchStore.getState();

      expect(client.get).toHaveBeenCalled();
      expect(state.memoryHistory).not.toBeNull();
      expect(state.memoryHistory!.memory_id).toBe("mem-1");
      expect(state.memoryHistory!.current_version).toBe(3);
      expect(state.memoryHistory!.versions).toHaveLength(3);
      expect(state.memoryHistory!.versions[0]!.version).toBe(1);
      expect(state.memoryHistory!.versions[0]!.status).toBe("superseded");
      expect(state.memoryHistory!.versions[2]!.version).toBe(3);
      expect(state.memoryHistory!.versions[2]!.status).toBe("active");
      expect(state.memoryHistoryLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("includes memory id in GET url", async () => {
      const client = mockClient({
        "/api/v2/memories/mem-42/history": {
          memory_id: "mem-42",
          current_version: 1,
          versions: [{ version: 1, created_at: "2025-01-01T00:00:00Z", status: "active" }],
        },
      });

      await useSearchStore.getState().fetchMemoryHistory("mem-42", client);

      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("/api/v2/memories/mem-42/history");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("History not available"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().fetchMemoryHistory("mem-1", client);
      const state = useSearchStore.getState();
      expect(state.memoryHistoryLoading).toBe(false);
      expect(state.memoryHistory).toBeNull();
      expect(state.error).toBe("History not available");
    });
  });

  describe("fetchMemoryDiff", () => {
    it("fetches diff from /api/v2/memories/{id}/diff with version params", async () => {
      const client = mockClient({
        "/api/v2/memories/mem-1/diff": {
          diff: "--- v1\n+++ v2\n-old content\n+new content",
          mode: "content",
          v1: 1,
          v2: 2,
        },
      });

      await useSearchStore.getState().fetchMemoryDiff("mem-1", 1, 2, client);
      const state = useSearchStore.getState();

      expect(client.get).toHaveBeenCalled();
      expect(state.memoryDiff).not.toBeNull();
      expect(state.memoryDiff!.diff).toBe("--- v1\n+++ v2\n-old content\n+new content");
      expect(state.memoryDiff!.mode).toBe("content");
      expect(state.memoryDiff!.v1).toBe(1);
      expect(state.memoryDiff!.v2).toBe(2);
      expect(state.memoryDiffLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("includes v1, v2, and mode params in GET url", async () => {
      const client = mockClient({
        "/api/v2/memories/mem-5/diff": {
          diff: "",
          mode: "content",
          v1: 3,
          v2: 5,
        },
      });

      await useSearchStore.getState().fetchMemoryDiff("mem-5", 3, 5, client);

      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("/api/v2/memories/mem-5/diff");
      expect(calledUrl).toContain("v1=3");
      expect(calledUrl).toContain("v2=5");
      expect(calledUrl).toContain("mode=content");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Diff generation failed"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().fetchMemoryDiff("mem-1", 1, 2, client);
      const state = useSearchStore.getState();
      expect(state.memoryDiffLoading).toBe(false);
      expect(state.memoryDiff).toBeNull();
      expect(state.error).toBe("Diff generation failed");
    });
  });

  describe("rollbackMemory", () => {
    it("posts rollback with version as query param and clears versioning state", async () => {
      useSearchStore.setState({
        memories: [
          { memory_id: "mem-1", content: "version 3 content", version: 3 },
        ],
        memoryHistory: {
          memory_id: "mem-1",
          current_version: 3,
          versions: [
            { version: 1, created_at: "2025-01-01T00:00:00Z", status: "superseded" },
            { version: 3, created_at: "2025-03-01T00:00:00Z", status: "active" },
          ],
        },
        memoryDiff: { diff: "some diff", mode: "content", v1: 2, v2: 3 },
      });

      // Backend returns { rolled_back, memory_id, rolled_back_to_version, current_version, content }
      const client = mockClient({
        "/api/v2/memories/mem-1/rollback": {
          rolled_back: true,
          memory_id: "mem-1",
          rolled_back_to_version: 1,
          current_version: 4,
          content: "version 1 content",
        },
      });

      await useSearchStore.getState().rollbackMemory("mem-1", 1, client);
      const state = useSearchStore.getState();

      expect(client.post).toHaveBeenCalled();
      // version is sent as query param
      const calledUrl = (client.post as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("version=1");
      // History and diff cleared after rollback
      expect(state.memoryHistory).toBeNull();
      expect(state.memoryDiff).toBeNull();
      expect(state.memoriesLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Rollback denied"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().rollbackMemory("mem-1", 1, client);
      const state = useSearchStore.getState();
      expect(state.memoriesLoading).toBe(false);
      expect(state.error).toBe("Rollback denied");
    });
  });

  describe("clearMemoryHistory", () => {
    it("clears memoryHistory state", () => {
      useSearchStore.setState({
        memoryHistory: {
          memory_id: "mem-1",
          current_version: 2,
          versions: [
            { version: 1, created_at: "2025-01-01T00:00:00Z", status: "superseded" },
            { version: 2, created_at: "2025-02-01T00:00:00Z", status: "active" },
          ],
        },
      });

      useSearchStore.getState().clearMemoryHistory();
      expect(useSearchStore.getState().memoryHistory).toBeNull();
    });
  });

  describe("askRlm", () => {
    /** Helper: build a mock SSE stream from event objects. */
    function buildSseStream(events: Array<{ event: string; data: unknown }>): ReadableStream<Uint8Array> {
      const encoder = new TextEncoder();
      const chunks = events.map(
        (e) => `event: ${e.event}\ndata: ${JSON.stringify(e.data)}\n\n`,
      );
      return new ReadableStream({
        start(controller) {
          for (const chunk of chunks) {
            controller.enqueue(encoder.encode(chunk));
          }
          controller.close();
        },
      });
    }

    function mockStreamingClient(
      stream: ReadableStream<Uint8Array>,
      status = 200,
    ): FetchClient {
      return {
        rawRequest: mock(async () => ({
          ok: status >= 200 && status < 300,
          status,
          body: stream,
          text: async () => "error",
        })),
      } as unknown as FetchClient;
    }

    it("streams SSE events and stores final answer with zone_id in body", async () => {
      const stream = buildSseStream([
        { event: "rlm.started", data: { query: "test", max_iterations: 15, model: "claude-sonnet-4-20250514" } },
        { event: "rlm.iteration", data: { step: 1, code_executed: "x = 1+1", output_summary: "2", tokens_used: 100, duration_seconds: 0.5 } },
        { event: "rlm.final_answer", data: { answer: "The answer is 42.", total_tokens: 500, total_duration_seconds: 2.0, iterations: 1 } },
      ]);

      const client = mockStreamingClient(stream);

      await useSearchStore.getState().askRlm("test", client, "zone-abc");
      const state = useSearchStore.getState();

      // Verify rawRequest was called with zone_id in body
      const calledBody = (client.rawRequest as ReturnType<typeof mock>).mock.calls[0]![2] as string;
      const parsed = JSON.parse(calledBody);
      expect(parsed.zone_id).toBe("zone-abc");
      expect(parsed.stream).toBe(true);

      expect(state.rlmAnswer).not.toBeNull();
      expect(state.rlmAnswer!.status).toBe("completed");
      expect(state.rlmAnswer!.answer).toBe("The answer is 42.");
      expect(state.rlmAnswer!.total_tokens).toBe(500);
      expect(state.rlmAnswer!.iterations).toBe(1);
      expect(state.rlmAnswer!.model).toBe("claude-sonnet-4-20250514");
      expect(state.rlmAnswer!.steps).toHaveLength(1);
      expect(state.rlmAnswer!.steps[0]!.step).toBe(1);
      expect(state.rlmLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("handles budget_exceeded SSE event", async () => {
      const stream = buildSseStream([
        { event: "rlm.started", data: { query: "test", max_iterations: 15, model: "gpt-4" } },
        { event: "rlm.budget_exceeded", data: { reason: "max_iterations", total_tokens: 5000, iterations: 15 } },
      ]);

      const client = mockStreamingClient(stream);

      await useSearchStore.getState().askRlm("test", client);
      const state = useSearchStore.getState();

      expect(state.rlmAnswer!.status).toBe("budget_exceeded");
      expect(state.rlmAnswer!.error_message).toBe("max_iterations");
      expect(state.rlmLoading).toBe(false);
    });

    it("handles rlm.error SSE event", async () => {
      const stream = buildSseStream([
        { event: "rlm.started", data: { query: "test", max_iterations: 15, model: "gpt-4" } },
        { event: "rlm.error", data: { error: "Sandbox unavailable" } },
      ]);

      const client = mockStreamingClient(stream);

      await useSearchStore.getState().askRlm("test", client);
      const state = useSearchStore.getState();

      expect(state.rlmAnswer!.status).toBe("error");
      expect(state.rlmAnswer!.error_message).toBe("Sandbox unavailable");
      expect(state.rlmLoading).toBe(false);
    });

    it("handles HTTP error from rawRequest", async () => {
      const stream = buildSseStream([]);
      const client = mockStreamingClient(stream, 503);

      await useSearchStore.getState().askRlm("test", client);
      const state = useSearchStore.getState();

      expect(state.rlmAnswer!.status).toBe("error");
      expect(state.rlmAnswer!.error_message).toContain("503");
      expect(state.rlmLoading).toBe(false);
    });

    it("sets error on network failure", async () => {
      const client = {
        rawRequest: mock(async () => { throw new Error("RLM service unavailable"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().askRlm("fail", client);
      const state = useSearchStore.getState();
      expect(state.rlmLoading).toBe(false);
      expect(state.error).toBe("RLM service unavailable");
    });

    it("omits zone_id from body when not provided", async () => {
      const stream = buildSseStream([
        { event: "rlm.final_answer", data: { answer: "ok", total_tokens: 10, total_duration_seconds: 0.1, iterations: 0 } },
      ]);
      const client = mockStreamingClient(stream);

      await useSearchStore.getState().askRlm("test", client);

      const calledBody = (client.rawRequest as ReturnType<typeof mock>).mock.calls[0]![2] as string;
      const parsed = JSON.parse(calledBody);
      expect(parsed.zone_id).toBeUndefined();
    });

    it("sends context_paths when rlmContextPaths is populated", async () => {
      useSearchStore.setState({
        rlmContextPaths: ["/data/doc1.md", "/data/doc2.py"],
      });

      const stream = buildSseStream([
        { event: "rlm.final_answer", data: { answer: "found it", total_tokens: 50, total_duration_seconds: 0.5, iterations: 1 } },
      ]);
      const client = mockStreamingClient(stream);

      await useSearchStore.getState().askRlm("what is in doc1?", client, "zone-x");

      const calledBody = (client.rawRequest as ReturnType<typeof mock>).mock.calls[0]![2] as string;
      const parsed = JSON.parse(calledBody);
      expect(parsed.context_paths).toEqual(["/data/doc1.md", "/data/doc2.py"]);
      expect(parsed.zone_id).toBe("zone-x");
    });

    it("omits context_paths when rlmContextPaths is empty", async () => {
      const stream = buildSseStream([
        { event: "rlm.final_answer", data: { answer: "ok", total_tokens: 10, total_duration_seconds: 0.1, iterations: 0 } },
      ]);
      const client = mockStreamingClient(stream);

      await useSearchStore.getState().askRlm("test", client);

      const calledBody = (client.rawRequest as ReturnType<typeof mock>).mock.calls[0]![2] as string;
      const parsed = JSON.parse(calledBody);
      expect(parsed.context_paths).toBeUndefined();
    });
  });

  describe("rlmContextPaths management", () => {
    it("adds a path to context", () => {
      useSearchStore.getState().addRlmContextPath("/data/file.txt");
      expect(useSearchStore.getState().rlmContextPaths).toEqual(["/data/file.txt"]);
    });

    it("prevents duplicate paths", () => {
      useSearchStore.getState().addRlmContextPath("/data/file.txt");
      useSearchStore.getState().addRlmContextPath("/data/file.txt");
      expect(useSearchStore.getState().rlmContextPaths).toEqual(["/data/file.txt"]);
    });

    it("removes a path from context", () => {
      useSearchStore.setState({ rlmContextPaths: ["/a", "/b", "/c"] });
      useSearchStore.getState().removeRlmContextPath("/b");
      expect(useSearchStore.getState().rlmContextPaths).toEqual(["/a", "/c"]);
    });

    it("clears all context paths", () => {
      useSearchStore.setState({ rlmContextPaths: ["/a", "/b"] });
      useSearchStore.getState().clearRlmContextPaths();
      expect(useSearchStore.getState().rlmContextPaths).toEqual([]);
    });
  });

  describe("clearMemoryDiff", () => {
    it("clears memoryDiff state", () => {
      useSearchStore.setState({
        memoryDiff: { diff: "some diff text", mode: "content", v1: 1, v2: 2 },
      });

      useSearchStore.getState().clearMemoryDiff();
      expect(useSearchStore.getState().memoryDiff).toBeNull();
    });
  });

  describe("error handling", () => {
    it("clears error when switching tabs", () => {
      useSearchStore.setState({ error: "previous error" });
      useSearchStore.getState().setActiveTab("memories");
      expect(useSearchStore.getState().error).toBeNull();
    });

    it("search clears previous error on success", async () => {
      useSearchStore.setState({ error: "old error" });

      const client = mockClient({
        "/v2/search/query": {
          query: "test",
          search_type: "hybrid",
          graph_mode: "none",
          results: [],
          total: 0,
          latency_ms: 1,
        },
      });

      await useSearchStore.getState().search("test", client);
      expect(useSearchStore.getState().error).toBeNull();
    });

  });

});
