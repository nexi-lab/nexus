import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useSearchStore } from "../../src/stores/search-store.js";
import type { FetchClient } from "@nexus/api-client";

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
    searchResults: [],
    searchTotal: 0,
    selectedResultIndex: 0,
    searchLoading: false,
    selectedEntity: null,
    neighbors: [],
    knowledgeSearchResult: null,
    knowledgeLoading: false,
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
      useSearchStore.getState().setActiveTab("knowledge");
      expect(useSearchStore.getState().activeTab).toBe("knowledge");
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
    it("uses GET with query string and stores results", async () => {
      const client = mockClient({
        "/api/v2/search/query": {
          query: "test",
          search_type: "hybrid",
          graph_mode: "none",
          results: [
            {
              path: "/data/test.txt",
              chunk_text: "This is a test file content",
              score: 0.95,
              chunk_index: 0,
              line_start: 1,
              line_end: 10,
              keyword_score: 0.8,
              vector_score: 0.9,
            },
            {
              path: "/data/another.py",
              chunk_text: "Another matching chunk",
              score: 0.82,
              chunk_index: 2,
              line_start: 15,
              line_end: 25,
              keyword_score: null,
              vector_score: 0.82,
            },
          ],
          total: 2,
          latency_ms: 42,
        },
      });

      await useSearchStore.getState().search("test", client);
      const state = useSearchStore.getState();

      // Verify GET was called (not POST)
      expect(client.get).toHaveBeenCalled();
      expect(client.post).not.toHaveBeenCalled();

      expect(state.searchResults).toHaveLength(2);
      expect(state.searchResults[0]!.path).toBe("/data/test.txt");
      expect(state.searchResults[0]!.chunk_text).toBe("This is a test file content");
      expect(state.searchResults[0]!.score).toBe(0.95);
      expect(state.searchResults[0]!.line_start).toBe(1);
      expect(state.searchResults[0]!.line_end).toBe(10);
      expect(state.searchResults[0]!.keyword_score).toBe(0.8);
      expect(state.searchResults[1]!.path).toBe("/data/another.py");
      expect(state.searchResults[1]!.chunk_index).toBe(2);
      expect(state.searchTotal).toBe(2);
      expect(state.selectedResultIndex).toBe(0);
      expect(state.searchQuery).toBe("test");
      expect(state.searchLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("includes q, type, and limit params in GET url", async () => {
      const client = mockClient({
        "/api/v2/search/query": {
          query: "hello",
          search_type: "hybrid",
          graph_mode: "none",
          results: [],
          total: 0,
          latency_ms: 5,
        },
      });

      await useSearchStore.getState().search("hello", client);

      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("q=hello");
      expect(calledUrl).toContain("type=hybrid");
      expect(calledUrl).toContain("limit=10");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Search service unavailable"); }),
        post: mock(async () => { throw new Error("unexpected"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().search("fail", client);
      const state = useSearchStore.getState();
      expect(state.searchLoading).toBe(false);
      expect(state.error).toBe("Search service unavailable");
    });
  });

  describe("fetchEntity", () => {
    it("fetches from /api/v2/graph/entity/ and unwraps entity field", async () => {
      const client = mockClient({
        "/api/v2/graph/entity/ent-1": {
          entity: {
            entity_id: "ent-1",
            type: "concept",
            name: "Machine Learning",
            properties: { domain: "AI", level: "advanced" },
          },
        },
      });

      await useSearchStore.getState().fetchEntity("ent-1", client);
      const state = useSearchStore.getState();

      expect(state.selectedEntity).not.toBeNull();
      expect((state.selectedEntity as Record<string, unknown>).entity_id).toBe("ent-1");
      expect((state.selectedEntity as Record<string, unknown>).name).toBe("Machine Learning");
      expect(state.knowledgeLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("stores null when entity is null in response", async () => {
      const client = mockClient({
        "/api/v2/graph/entity/missing": {
          entity: null,
        },
      });

      await useSearchStore.getState().fetchEntity("missing", client);
      const state = useSearchStore.getState();
      expect(state.selectedEntity).toBeNull();
      expect(state.knowledgeLoading).toBe(false);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Entity not found"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().fetchEntity("missing", client);
      const state = useSearchStore.getState();
      expect(state.knowledgeLoading).toBe(false);
      expect(state.error).toBe("Entity not found");
    });
  });

  describe("fetchNeighbors", () => {
    it("fetches from /api/v2/graph/entity/{id}/neighbors with depth info", async () => {
      const client = mockClient({
        "/api/v2/graph/entity/ent-1/neighbors": {
          neighbors: [
            {
              entity: { entity_id: "ent-2", type: "concept", name: "Deep Learning" },
              depth: 1,
              path: ["ent-1", "ent-2"],
            },
            {
              entity: { entity_id: "ent-3", type: "tool", name: "TensorFlow" },
              depth: 1,
              path: ["ent-1", "ent-3"],
            },
          ],
        },
      });

      await useSearchStore.getState().fetchNeighbors("ent-1", client);
      const state = useSearchStore.getState();

      expect(state.neighbors).toHaveLength(2);
      expect(state.neighbors[0]!.entity.entity_id).toBe("ent-2");
      expect(state.neighbors[0]!.depth).toBe(1);
      expect(state.neighbors[0]!.path).toEqual(["ent-1", "ent-2"]);
      expect(state.neighbors[1]!.entity.name).toBe("TensorFlow");
      expect(state.knowledgeLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("includes hops and direction params in url", async () => {
      const client = mockClient({
        "/api/v2/graph/entity/ent-1/neighbors": {
          neighbors: [],
        },
      });

      await useSearchStore.getState().fetchNeighbors("ent-1", client);

      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("hops=1");
      expect(calledUrl).toContain("direction=both");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Graph unavailable"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().fetchNeighbors("ent-1", client);
      const state = useSearchStore.getState();
      expect(state.neighbors).toEqual([]);
      expect(state.knowledgeLoading).toBe(false);
      expect(state.error).toBe("Graph unavailable");
    });
  });

  describe("searchKnowledge", () => {
    it("uses GET on /api/v2/graph/search and returns single entity", async () => {
      const client = mockClient({
        "/api/v2/graph/search": {
          entity: {
            entity_id: "ent-10",
            type: "service",
            name: "Auth Service",
            properties: { port: 8080 },
          },
        },
      });

      await useSearchStore.getState().searchKnowledge("Auth Service", client);
      const state = useSearchStore.getState();

      // Verify GET was called (not POST)
      expect(client.get).toHaveBeenCalled();
      expect(client.post).not.toHaveBeenCalled();

      expect(state.knowledgeSearchResult).not.toBeNull();
      expect((state.knowledgeSearchResult as Record<string, unknown>).entity_id).toBe("ent-10");
      expect((state.knowledgeSearchResult as Record<string, unknown>).name).toBe("Auth Service");
      expect(state.knowledgeLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("includes name and fuzzy params in GET url", async () => {
      const client = mockClient({
        "/api/v2/graph/search": { entity: null },
      });

      await useSearchStore.getState().searchKnowledge("test", client);

      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("name=test");
      expect(calledUrl).toContain("fuzzy=false");
    });

    it("stores null when entity not found", async () => {
      const client = mockClient({
        "/api/v2/graph/search": { entity: null },
      });

      await useSearchStore.getState().searchKnowledge("nonexistent", client);
      const state = useSearchStore.getState();
      expect(state.knowledgeSearchResult).toBeNull();
      expect(state.knowledgeLoading).toBe(false);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Knowledge search failed"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().searchKnowledge("fail", client);
      const state = useSearchStore.getState();
      expect(state.knowledgeSearchResult).toBeNull();
      expect(state.knowledgeLoading).toBe(false);
      expect(state.error).toBe("Knowledge search failed");
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
              scope: "team",
              tags: ["deploy", "ci"],
              steps: [{ action: "build" }, { action: "push" }],
              metadata: { author: "alice" },
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-06-15T12:00:00Z",
              usage_count: 42,
              success_rate: 0.95,
            },
            {
              playbook_id: "pb-2",
              name: "Onboard Agent",
              description: "New agent onboarding",
              scope: "global",
              tags: ["onboard"],
              steps: [{ action: "register" }],
              metadata: null,
              created_at: "2025-02-01T00:00:00Z",
              updated_at: "2025-07-01T08:00:00Z",
              usage_count: 7,
              success_rate: 1.0,
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
      expect(state.playbooks[0]!.tags).toEqual(["deploy", "ci"]);
      expect(state.playbooks[0]!.usage_count).toBe(42);
      expect(state.playbooks[0]!.success_rate).toBe(0.95);
      expect(state.playbooks[1]!.playbook_id).toBe("pb-2");
      expect(state.playbooks[1]!.metadata).toBeNull();
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
            scope: "team",
            tags: ["deploy"],
            steps: [],
            metadata: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
            usage_count: 5,
            success_rate: 0.9,
          },
          {
            playbook_id: "pb-2",
            name: "Onboard Agent",
            description: "desc",
            scope: "global",
            tags: [],
            steps: [],
            metadata: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
            usage_count: 3,
            success_rate: 1.0,
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
            scope: "team",
            tags: [],
            steps: [],
            metadata: null,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
            usage_count: 1,
            success_rate: 1.0,
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
    it("posts rollback and updates memory in local list", async () => {
      useSearchStore.setState({
        memories: [
          { memory_id: "mem-1", content: "version 3 content", version: 3 },
          { memory_id: "mem-2", content: "other memory", version: 1 },
        ],
        memoryHistory: {
          memory_id: "mem-1",
          current_version: 3,
          versions: [
            { version: 1, created_at: "2025-01-01T00:00:00Z", status: "superseded" },
            { version: 2, created_at: "2025-02-01T00:00:00Z", status: "superseded" },
            { version: 3, created_at: "2025-03-01T00:00:00Z", status: "active" },
          ],
        },
        memoryDiff: { diff: "some diff", mode: "content", v1: 2, v2: 3 },
      });

      const client = mockClient({
        "/api/v2/memories/mem-1/rollback": {
          memory: { memory_id: "mem-1", content: "version 1 content", version: 1 },
        },
      });

      await useSearchStore.getState().rollbackMemory("mem-1", 1, client);
      const state = useSearchStore.getState();

      expect(client.post).toHaveBeenCalled();
      expect(state.memories).toHaveLength(2);
      expect((state.memories[0] as Record<string, unknown>).content).toBe("version 1 content");
      expect((state.memories[0] as Record<string, unknown>).version).toBe(1);
      // Other memory unchanged
      expect((state.memories[1] as Record<string, unknown>).content).toBe("other memory");
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
        "/api/v2/search/query": {
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

    it("fetchEntity clears previous error on success", async () => {
      useSearchStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/graph/entity/ent-1": {
          entity: {
            entity_id: "ent-1",
            type: "concept",
            name: "Test",
          },
        },
      });

      await useSearchStore.getState().fetchEntity("ent-1", client);
      expect(useSearchStore.getState().error).toBeNull();
    });
  });
});
