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
  } as unknown as FetchClient;
}

function resetStore(): void {
  useSearchStore.setState({
    searchQuery: "",
    searchResults: [],
    searchTotal: 0,
    selectedResultIndex: 0,
    searchLoading: false,
    entities: [],
    selectedEntity: null,
    neighbors: [],
    knowledgeLoading: false,
    memories: [],
    selectedMemoryIndex: 0,
    memoriesLoading: false,
    playbooks: [],
    playbooksLoading: false,
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

      useSearchStore.getState().setActiveTab("playbooks");
      expect(useSearchStore.getState().activeTab).toBe("playbooks");

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
    it("searches and stores results", async () => {
      const client = mockClient({
        "/api/v2/search/query": {
          results: [
            {
              id: "r1",
              type: "file",
              path: "/data/test.txt",
              title: "Test File",
              snippet: "This is a test file content",
              score: 0.95,
              zone_id: "zone-1",
            },
            {
              id: "r2",
              type: "entity",
              path: null,
              title: "Test Entity",
              snippet: "An entity matching the query",
              score: 0.82,
              zone_id: null,
            },
          ],
          total: 2,
        },
      });

      await useSearchStore.getState().search("test", client);
      const state = useSearchStore.getState();

      expect(state.searchResults).toHaveLength(2);
      expect(state.searchResults[0]!.id).toBe("r1");
      expect(state.searchResults[0]!.type).toBe("file");
      expect(state.searchResults[0]!.score).toBe(0.95);
      expect(state.searchResults[1]!.title).toBe("Test Entity");
      expect(state.searchTotal).toBe(2);
      expect(state.selectedResultIndex).toBe(0);
      expect(state.searchQuery).toBe("test");
      expect(state.searchLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Search service unavailable"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().search("fail", client);
      const state = useSearchStore.getState();
      expect(state.searchLoading).toBe(false);
      expect(state.error).toBe("Search service unavailable");
    });
  });

  describe("fetchEntity", () => {
    it("fetches and stores entity detail", async () => {
      const client = mockClient({
        "/api/v2/knowledge/entity/ent-1": {
          entity_id: "ent-1",
          type: "concept",
          name: "Machine Learning",
          properties: { domain: "AI", level: "advanced" },
          created_at: "2025-01-01T00:00:00Z",
          updated_at: "2025-06-01T00:00:00Z",
        },
      });

      await useSearchStore.getState().fetchEntity("ent-1", client);
      const state = useSearchStore.getState();

      expect(state.selectedEntity).not.toBeNull();
      expect(state.selectedEntity!.entity_id).toBe("ent-1");
      expect(state.selectedEntity!.name).toBe("Machine Learning");
      expect(state.selectedEntity!.type).toBe("concept");
      expect(state.selectedEntity!.properties).toEqual({ domain: "AI", level: "advanced" });
      expect(state.knowledgeLoading).toBe(false);
      expect(state.error).toBeNull();
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
    it("fetches and stores neighbors", async () => {
      const client = mockClient({
        "/api/v2/knowledge/entity/ent-1/neighbors": {
          neighbors: [
            {
              entity_id: "ent-2",
              type: "concept",
              name: "Deep Learning",
              properties: {},
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-02-01T00:00:00Z",
            },
            {
              entity_id: "ent-3",
              type: "tool",
              name: "TensorFlow",
              properties: { version: "2.0" },
              created_at: "2025-01-15T00:00:00Z",
              updated_at: "2025-03-01T00:00:00Z",
            },
          ],
        },
      });

      await useSearchStore.getState().fetchNeighbors("ent-1", client);
      const state = useSearchStore.getState();

      expect(state.neighbors).toHaveLength(2);
      expect(state.neighbors[0]!.entity_id).toBe("ent-2");
      expect(state.neighbors[0]!.name).toBe("Deep Learning");
      expect(state.neighbors[1]!.name).toBe("TensorFlow");
      expect(state.knowledgeLoading).toBe(false);
      expect(state.error).toBeNull();
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
    it("searches knowledge graph and stores entities", async () => {
      const client = mockClient({
        "/api/v2/knowledge/search": {
          entities: [
            {
              entity_id: "ent-10",
              type: "service",
              name: "Auth Service",
              properties: { port: 8080 },
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-04-01T00:00:00Z",
            },
          ],
        },
      });

      await useSearchStore.getState().searchKnowledge("auth", client);
      const state = useSearchStore.getState();

      expect(state.entities).toHaveLength(1);
      expect(state.entities[0]!.entity_id).toBe("ent-10");
      expect(state.entities[0]!.name).toBe("Auth Service");
      expect(state.knowledgeLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Knowledge search failed"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().searchKnowledge("fail", client);
      const state = useSearchStore.getState();
      expect(state.entities).toEqual([]);
      expect(state.knowledgeLoading).toBe(false);
      expect(state.error).toBe("Knowledge search failed");
    });
  });

  describe("fetchMemories", () => {
    it("fetches and stores memories", async () => {
      const client = mockClient({
        "/api/v2/memory": {
          memories: [
            {
              memory_id: "mem-1",
              agent_id: "agent-1",
              type: "episodic",
              content: "User asked about deployment steps",
              tags: ["deployment", "docs"],
              version: 2,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-02T00:00:00Z",
            },
            {
              memory_id: "mem-2",
              agent_id: "agent-2",
              type: "semantic",
              content: "API rate limits are 100 req/s",
              tags: ["api"],
              version: 1,
              created_at: "2025-01-03T00:00:00Z",
              updated_at: "2025-01-03T00:00:00Z",
            },
          ],
          total: 2,
        },
      });

      await useSearchStore.getState().fetchMemories(client);
      const state = useSearchStore.getState();

      expect(state.memories).toHaveLength(2);
      expect(state.memories[0]!.memory_id).toBe("mem-1");
      expect(state.memories[0]!.agent_id).toBe("agent-1");
      expect(state.memories[0]!.type).toBe("episodic");
      expect(state.memories[0]!.tags).toEqual(["deployment", "docs"]);
      expect(state.memories[0]!.version).toBe(2);
      expect(state.memories[1]!.content).toBe("API rate limits are 100 req/s");
      expect(state.selectedMemoryIndex).toBe(0);
      expect(state.memoriesLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Memory service down"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().fetchMemories(client);
      const state = useSearchStore.getState();
      expect(state.memoriesLoading).toBe(false);
      expect(state.error).toBe("Memory service down");
    });
  });

  describe("fetchMemoryDetail", () => {
    it("fetches detail and updates matching memory in list", async () => {
      useSearchStore.setState({
        memories: [
          {
            memory_id: "mem-1",
            agent_id: "agent-1",
            type: "episodic",
            content: "short",
            tags: [],
            version: 1,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
          },
        ],
      });

      const client = mockClient({
        "/api/v2/memory/mem-1": {
          memory_id: "mem-1",
          agent_id: "agent-1",
          type: "episodic",
          content: "Full detailed content of the memory",
          tags: ["updated"],
          version: 2,
          created_at: "2025-01-01T00:00:00Z",
          updated_at: "2025-01-05T00:00:00Z",
        },
      });

      await useSearchStore.getState().fetchMemoryDetail("mem-1", client);
      const state = useSearchStore.getState();

      expect(state.memories[0]!.content).toBe("Full detailed content of the memory");
      expect(state.memories[0]!.version).toBe(2);
      expect(state.memories[0]!.tags).toEqual(["updated"]);
      expect(state.memoriesLoading).toBe(false);
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
    it("fetches and stores playbooks", async () => {
      const client = mockClient({
        "/api/v2/playbooks": {
          playbooks: [
            {
              playbook_id: "pb-1",
              name: "Deploy Pipeline",
              description: "Automated deployment workflow",
              steps: 5,
              last_run: "2025-06-01T12:00:00Z",
              status: "active",
            },
            {
              playbook_id: "pb-2",
              name: "Onboarding",
              description: "New agent onboarding steps",
              steps: 3,
              last_run: null,
              status: "draft",
            },
            {
              playbook_id: "pb-3",
              name: "Old Cleanup",
              description: "Legacy cleanup procedure",
              steps: 8,
              last_run: "2024-12-01T00:00:00Z",
              status: "archived",
            },
          ],
        },
      });

      await useSearchStore.getState().fetchPlaybooks(client);
      const state = useSearchStore.getState();

      expect(state.playbooks).toHaveLength(3);
      expect(state.playbooks[0]!.playbook_id).toBe("pb-1");
      expect(state.playbooks[0]!.name).toBe("Deploy Pipeline");
      expect(state.playbooks[0]!.steps).toBe(5);
      expect(state.playbooks[0]!.status).toBe("active");
      expect(state.playbooks[1]!.status).toBe("draft");
      expect(state.playbooks[1]!.last_run).toBeNull();
      expect(state.playbooks[2]!.status).toBe("archived");
      expect(state.playbooksLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Playbook service unavailable"); }),
      } as unknown as FetchClient;

      await useSearchStore.getState().fetchPlaybooks(client);
      const state = useSearchStore.getState();
      expect(state.playbooksLoading).toBe(false);
      expect(state.error).toBe("Playbook service unavailable");
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
        "/api/v2/search/query": { results: [], total: 0 },
      });

      await useSearchStore.getState().search("test", client);
      expect(useSearchStore.getState().error).toBeNull();
    });

    it("fetchEntity clears previous error on success", async () => {
      useSearchStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/knowledge/entity/ent-1": {
          entity_id: "ent-1",
          type: "concept",
          name: "Test",
          properties: {},
          created_at: "2025-01-01T00:00:00Z",
          updated_at: "2025-01-01T00:00:00Z",
        },
      });

      await useSearchStore.getState().fetchEntity("ent-1", client);
      expect(useSearchStore.getState().error).toBeNull();
    });
  });
});
