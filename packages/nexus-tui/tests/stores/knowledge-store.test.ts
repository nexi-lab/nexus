/**
 * Tests for knowledge store — aspects, schemas, MCL replay, column search.
 * Issue #2930.
 */

import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useKnowledgeStore } from "../../src/stores/knowledge-store.js";
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
    patch: mock(async (path: string) => {
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
    deleteNoContent: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return;
      }
      throw new Error(`Unmocked path: ${path}`);
    }),
  } as unknown as FetchClient;
}

function resetStore(): void {
  useKnowledgeStore.setState({
    aspectsCache: new Map(),
    aspectDetailCache: new Map(),
    aspectsLoading: false,
    schemaCache: new Map(),
    schemaLoading: false,
    replayEntries: [],
    replayLoading: false,
    replayHasMore: false,
    replayNextCursor: 0,
    columnSearchResults: [],
    columnSearchLoading: false,
    error: null,
  });
}

describe("KnowledgeStore", () => {
  beforeEach(() => {
    resetStore();
  });

  describe("fetchAspects", () => {
    it("fetches and caches aspects by URN", async () => {
      const client = mockClient({
        "/api/v2/aspects/": {
          aspects: ["ownership", "schema", "tags"],
        },
      });

      await useKnowledgeStore.getState().fetchAspects("urn:li:dataset:1", client);
      const state = useKnowledgeStore.getState();

      expect(state.aspectsCache.get("urn:li:dataset:1")).toEqual([
        "ownership",
        "schema",
        "tags",
      ]);
      expect(state.aspectsLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("does not re-fetch cached URN", async () => {
      const existing = new Map([["urn:li:dataset:1", ["ownership"]]]);
      useKnowledgeStore.setState({ aspectsCache: existing });

      const client = mockClient({});
      await useKnowledgeStore.getState().fetchAspects("urn:li:dataset:1", client);

      // get was never called because the URN is cached
      expect((client.get as ReturnType<typeof mock>).mock.calls.length).toBe(0);
      expect(useKnowledgeStore.getState().aspectsCache.get("urn:li:dataset:1")).toEqual([
        "ownership",
      ]);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Aspects unavailable");
        }),
      } as unknown as FetchClient;

      await useKnowledgeStore.getState().fetchAspects("urn:bad", client);
      expect(useKnowledgeStore.getState().aspectsLoading).toBe(false);
      expect(useKnowledgeStore.getState().error).toBe("Aspects unavailable");
    });

    it("handles non-Error thrown objects", async () => {
      const client = {
        get: mock(async () => {
          throw "string error";
        }),
      } as unknown as FetchClient;

      await useKnowledgeStore.getState().fetchAspects("urn:bad", client);
      expect(useKnowledgeStore.getState().error).toBe("Failed to fetch aspects");
    });
  });

  describe("fetchAspectDetail", () => {
    it("fetches and caches aspect detail by URN::name key", async () => {
      const client = mockClient({
        "/api/v2/aspects/": {
          aspectName: "ownership",
          version: 3,
          payload: { owners: ["agent-1"] },
          createdBy: "agent-1",
        },
      });

      await useKnowledgeStore
        .getState()
        .fetchAspectDetail("urn:li:dataset:1", "ownership", client);
      const state = useKnowledgeStore.getState();

      const detail = state.aspectDetailCache.get("urn:li:dataset:1::ownership");
      expect(detail).toBeDefined();
      expect(detail!.name).toBe("ownership");
      expect(detail!.version).toBe(3);
      expect(detail!.payload).toEqual({ owners: ["agent-1"] });
      expect(detail!.createdBy).toBe("agent-1");
      expect(state.aspectsLoading).toBe(false);
    });

    it("does not re-fetch cached detail", async () => {
      const existing = new Map([
        [
          "urn:li:dataset:1::ownership",
          {
            name: "ownership",
            payload: {},
            version: 1,
            createdBy: "system",
          },
        ],
      ]);
      useKnowledgeStore.setState({ aspectDetailCache: existing });

      const client = mockClient({});
      await useKnowledgeStore
        .getState()
        .fetchAspectDetail("urn:li:dataset:1", "ownership", client);

      expect((client.get as ReturnType<typeof mock>).mock.calls.length).toBe(0);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Detail unavailable");
        }),
      } as unknown as FetchClient;

      await useKnowledgeStore
        .getState()
        .fetchAspectDetail("urn:bad", "ownership", client);
      expect(useKnowledgeStore.getState().error).toBe("Detail unavailable");
    });
  });

  describe("fetchSchema", () => {
    it("fetches and caches schema by path", async () => {
      const client = mockClient({
        "/api/v2/catalog/schema/": {
          schema: {
            columns: [
              { name: "id", type: "int64", nullable: "false" },
              { name: "name", type: "string", nullable: "true" },
            ],
            format: "parquet",
            rowCount: 1000,
            confidence: 0.95,
          },
        },
      });

      await useKnowledgeStore.getState().fetchSchema("/data/table.parquet", client);
      const state = useKnowledgeStore.getState();

      const schema = state.schemaCache.get("/data/table.parquet");
      expect(schema).toBeDefined();
      expect(schema!.columns).toHaveLength(2);
      expect(schema!.columns[0]!.name).toBe("id");
      expect(schema!.columns[1]!.type).toBe("string");
      expect(schema!.format).toBe("parquet");
      expect(schema!.rowCount).toBe(1000);
      expect(schema!.confidence).toBe(0.95);
      expect(state.schemaLoading).toBe(false);
    });

    it("handles null schema (file with no detected schema)", async () => {
      const client = mockClient({
        "/api/v2/catalog/schema/": { schema: null },
      });

      await useKnowledgeStore.getState().fetchSchema("/data/unknown.bin", client);
      const state = useKnowledgeStore.getState();

      expect(state.schemaCache.has("/data/unknown.bin")).toBe(true);
      expect(state.schemaCache.get("/data/unknown.bin")).toBeNull();
      expect(state.schemaLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("does not re-fetch cached schema", async () => {
      const existing = new Map<string, null>([["/data/file.csv", null]]);
      useKnowledgeStore.setState({ schemaCache: existing });

      const client = mockClient({});
      await useKnowledgeStore.getState().fetchSchema("/data/file.csv", client);

      expect((client.get as ReturnType<typeof mock>).mock.calls.length).toBe(0);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Schema unavailable");
        }),
      } as unknown as FetchClient;

      await useKnowledgeStore.getState().fetchSchema("/bad/path", client);
      expect(useKnowledgeStore.getState().error).toBe("Schema unavailable");
    });
  });

  describe("fetchReplay", () => {
    it("fetches replay entries and stores them", async () => {
      const client = mockClient({
        "/api/v2/ops/replay": {
          records: [
            {
              sequenceNumber: 1,
              entityUrn: "urn:li:dataset:1",
              aspectName: "ownership",
              changeType: "UPSERT",
              timestamp: "2025-01-01T00:00:00Z",
            },
            {
              sequenceNumber: 2,
              entityUrn: "urn:li:dataset:2",
              aspectName: "schema",
              changeType: "CREATE",
              timestamp: "2025-01-01T00:01:00Z",
            },
          ],
          nextCursor: 3,
          hasMore: true,
        },
      });

      await useKnowledgeStore.getState().fetchReplay(client);
      const state = useKnowledgeStore.getState();

      expect(state.replayEntries).toHaveLength(2);
      expect(state.replayEntries[0]!.sequenceNumber).toBe(1);
      expect(state.replayEntries[0]!.entityUrn).toBe("urn:li:dataset:1");
      expect(state.replayEntries[1]!.changeType).toBe("CREATE");
      expect(state.replayHasMore).toBe(true);
      expect(state.replayNextCursor).toBe(3);
      expect(state.replayLoading).toBe(false);
    });

    it("replaces entries on fresh fetch (fromSequence=0)", async () => {
      useKnowledgeStore.setState({
        replayEntries: [
          {
            sequenceNumber: 99,
            entityUrn: "urn:old",
            aspectName: "old",
            changeType: "DELETE",
            timestamp: "2024-01-01T00:00:00Z",
          },
        ],
      });

      const client = mockClient({
        "/api/v2/ops/replay": {
          records: [
            {
              sequenceNumber: 1,
              entityUrn: "urn:new",
              aspectName: "fresh",
              changeType: "CREATE",
              timestamp: "2025-06-01T00:00:00Z",
            },
          ],
          nextCursor: 2,
          hasMore: false,
        },
      });

      await useKnowledgeStore.getState().fetchReplay(client, 0, 50);
      const state = useKnowledgeStore.getState();

      expect(state.replayEntries).toHaveLength(1);
      expect(state.replayEntries[0]!.entityUrn).toBe("urn:new");
    });

    it("appends entries on cursor continuation (fromSequence > 0)", async () => {
      useKnowledgeStore.setState({
        replayEntries: [
          {
            sequenceNumber: 1,
            entityUrn: "urn:first",
            aspectName: "first",
            changeType: "CREATE",
            timestamp: "2025-01-01T00:00:00Z",
          },
        ],
      });

      const client = mockClient({
        "/api/v2/ops/replay": {
          records: [
            {
              sequenceNumber: 2,
              entityUrn: "urn:second",
              aspectName: "second",
              changeType: "UPDATE",
              timestamp: "2025-01-01T00:01:00Z",
            },
          ],
          nextCursor: 3,
          hasMore: true,
        },
      });

      await useKnowledgeStore.getState().fetchReplay(client, 2, 50);
      const state = useKnowledgeStore.getState();

      expect(state.replayEntries).toHaveLength(2);
      expect(state.replayEntries[0]!.entityUrn).toBe("urn:first");
      expect(state.replayEntries[1]!.entityUrn).toBe("urn:second");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Replay unavailable");
        }),
      } as unknown as FetchClient;

      await useKnowledgeStore.getState().fetchReplay(client);
      expect(useKnowledgeStore.getState().error).toBe("Replay unavailable");
      expect(useKnowledgeStore.getState().replayLoading).toBe(false);
    });
  });

  describe("searchByColumn", () => {
    it("stores search results", async () => {
      const client = mockClient({
        "/api/v2/catalog/search": {
          results: [
            {
              entityUrn: "urn:li:dataset:1",
              columnName: "user_id",
              columnType: "int64",
            },
            {
              entityUrn: "urn:li:dataset:2",
              columnName: "user_id",
              columnType: "string",
            },
          ],
        },
      });

      await useKnowledgeStore.getState().searchByColumn("user_id", client);
      const state = useKnowledgeStore.getState();

      expect(state.columnSearchResults).toHaveLength(2);
      expect(state.columnSearchResults[0]!.entityUrn).toBe("urn:li:dataset:1");
      expect(state.columnSearchResults[0]!.columnName).toBe("user_id");
      expect(state.columnSearchResults[1]!.columnType).toBe("string");
      expect(state.columnSearchLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Search unavailable");
        }),
      } as unknown as FetchClient;

      await useKnowledgeStore.getState().searchByColumn("bad_col", client);
      expect(useKnowledgeStore.getState().error).toBe("Search unavailable");
      expect(useKnowledgeStore.getState().columnSearchLoading).toBe(false);
    });

    it("handles non-Error thrown objects", async () => {
      const client = {
        get: mock(async () => {
          throw "string error";
        }),
      } as unknown as FetchClient;

      await useKnowledgeStore.getState().searchByColumn("col", client);
      expect(useKnowledgeStore.getState().error).toBe("Failed to search");
    });
  });

  describe("clearReplay", () => {
    it("resets replay state", () => {
      useKnowledgeStore.setState({
        replayEntries: [
          {
            sequenceNumber: 1,
            entityUrn: "urn:test",
            aspectName: "test",
            changeType: "CREATE",
            timestamp: "2025-01-01T00:00:00Z",
          },
        ],
        replayNextCursor: 5,
        replayHasMore: true,
      });

      useKnowledgeStore.getState().clearReplay();
      const state = useKnowledgeStore.getState();

      expect(state.replayEntries).toHaveLength(0);
      expect(state.replayNextCursor).toBe(0);
      expect(state.replayHasMore).toBe(false);
    });
  });
});
