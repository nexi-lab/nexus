import { describe, it, expect, beforeEach, mock } from "bun:test";
import {
  useVersionsStore,
  nextStatusFilter,
} from "../../src/stores/versions-store.js";
import type { FetchClient } from "@nexus-ai-fs/api-client";

// =============================================================================
// Helpers
// =============================================================================

function mockClient(responses: Record<string, unknown>): FetchClient {
  return {
    get: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked GET path: ${path}`);
    }),
    post: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked POST path: ${path}`);
    }),
  } as unknown as FetchClient;
}

const SAMPLE_TRANSACTION = {
  transaction_id: "txn-001",
  zone_id: "zone-a",
  agent_id: null,
  status: "active" as const,
  description: "test snapshot",
  created_at: "2026-01-01T00:00:00Z",
  expires_at: "2026-01-01T01:00:00Z",
  entry_count: 2,
};

const SAMPLE_ENTRY = {
  entry_id: "entry-001",
  transaction_id: "txn-001",
  path: "/data/file.txt",
  operation: "write" as const,
  original_hash: "abc12345",
  new_hash: "def67890",
  created_at: "2026-01-01T00:00:00Z",
};

function resetStore(): void {
  useVersionsStore.setState({
    transactions: [],
    selectedTransaction: null,
    selectedIndex: 0,
    statusFilter: null,
    isLoading: false,
    error: null,
    entries: [],
    entriesLoading: false,
  });
}

// =============================================================================
// Tests
// =============================================================================

describe("VersionsStore", () => {
  beforeEach(() => {
    resetStore();
  });

  describe("fetchTransactions", () => {
    it("fetches and stores transactions", async () => {
      const client = mockClient({
        "/api/v2/snapshots": {
          transactions: [SAMPLE_TRANSACTION],
          count: 1,
        },
      });

      await useVersionsStore.getState().fetchTransactions(client);

      const state = useVersionsStore.getState();
      expect(state.transactions).toHaveLength(1);
      expect(state.transactions[0]!.transaction_id).toBe("txn-001");
      expect(state.isLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("includes status filter in query param", async () => {
      const getMock = mock(async () => ({
        transactions: [],
        count: 0,
      }));

      const client = { get: getMock, post: mock(async () => ({})) } as unknown as FetchClient;

      useVersionsStore.setState({ statusFilter: "active" });
      await useVersionsStore.getState().fetchTransactions(client);

      const calledPath = getMock.mock.calls[0]![0] as string;
      expect(calledPath).toContain("status=active");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Network error");
        }),
        post: mock(async () => ({})),
      } as unknown as FetchClient;

      await useVersionsStore.getState().fetchTransactions(client);

      const state = useVersionsStore.getState();
      expect(state.error).toBe("Network error");
      expect(state.isLoading).toBe(false);
    });
  });

  describe("selectTransaction", () => {
    it("sets selected transaction and index", () => {
      useVersionsStore.setState({
        transactions: [SAMPLE_TRANSACTION],
      });

      useVersionsStore.getState().selectTransaction(SAMPLE_TRANSACTION);

      const state = useVersionsStore.getState();
      expect(state.selectedTransaction).toEqual(SAMPLE_TRANSACTION);
      expect(state.selectedIndex).toBe(0);
    });

    it("clears entries when selecting a new transaction", () => {
      useVersionsStore.setState({
        transactions: [SAMPLE_TRANSACTION],
        entries: [SAMPLE_ENTRY],
      });

      useVersionsStore.getState().selectTransaction(SAMPLE_TRANSACTION);

      expect(useVersionsStore.getState().entries).toEqual([]);
    });
  });

  describe("setSelectedIndex", () => {
    it("updates index and selected transaction", () => {
      const txn2 = { ...SAMPLE_TRANSACTION, transaction_id: "txn-002" };
      useVersionsStore.setState({
        transactions: [SAMPLE_TRANSACTION, txn2],
      });

      useVersionsStore.getState().setSelectedIndex(1);

      const state = useVersionsStore.getState();
      expect(state.selectedIndex).toBe(1);
      expect(state.selectedTransaction?.transaction_id).toBe("txn-002");
    });

    it("sets null when index is out of bounds", () => {
      useVersionsStore.setState({ transactions: [] });

      useVersionsStore.getState().setSelectedIndex(5);

      expect(useVersionsStore.getState().selectedTransaction).toBeNull();
    });
  });

  describe("setStatusFilter", () => {
    it("updates filter and resets selection", () => {
      useVersionsStore.setState({
        selectedIndex: 3,
        selectedTransaction: SAMPLE_TRANSACTION,
      });

      useVersionsStore.getState().setStatusFilter("committed");

      const state = useVersionsStore.getState();
      expect(state.statusFilter).toBe("committed");
      expect(state.selectedIndex).toBe(0);
      expect(state.selectedTransaction).toBeNull();
    });

    it("can be set to null for no filter", () => {
      useVersionsStore.setState({ statusFilter: "active" });

      useVersionsStore.getState().setStatusFilter(null);

      expect(useVersionsStore.getState().statusFilter).toBeNull();
    });
  });

  describe("fetchEntries", () => {
    it("fetches entries for a transaction", async () => {
      const client = mockClient({
        "/api/v2/snapshots/txn-001/entries": [SAMPLE_ENTRY],
      });

      await useVersionsStore.getState().fetchEntries("txn-001", client);

      const state = useVersionsStore.getState();
      expect(state.entries).toHaveLength(1);
      expect(state.entries[0]!.path).toBe("/data/file.txt");
      expect(state.entriesLoading).toBe(false);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Entries not found");
        }),
        post: mock(async () => ({})),
      } as unknown as FetchClient;

      await useVersionsStore.getState().fetchEntries("txn-999", client);

      const state = useVersionsStore.getState();
      expect(state.entries).toEqual([]);
      expect(state.entriesLoading).toBe(false);
      expect(state.error).toBe("Entries not found");
    });
  });

  describe("beginTransaction", () => {
    it("posts to create and refreshes list", async () => {
      const postMock = mock(async () => ({
        transaction_id: "txn-new",
        zone_id: "zone-a",
        agent_id: null,
        status: "active",
        description: null,
        created_at: "2026-01-01T00:00:00Z",
        expires_at: "2026-01-01T01:00:00Z",
        entry_count: 0,
      }));

      const getMock = mock(async () => ({
        transactions: [],
        count: 0,
      }));

      const client = { post: postMock, get: getMock } as unknown as FetchClient;

      await useVersionsStore.getState().beginTransaction(client, "my snapshot", 3600);

      // Verify POST was called
      expect(postMock.mock.calls).toHaveLength(1);
      const postPath = postMock.mock.calls[0]![0] as string;
      expect(postPath).toBe("/api/v2/snapshots");

      // Verify GET was called to refresh
      expect(getMock.mock.calls).toHaveLength(1);
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => {
          throw new Error("Quota exceeded");
        }),
        get: mock(async () => ({ transactions: [], count: 0 })),
      } as unknown as FetchClient;

      await useVersionsStore.getState().beginTransaction(client);

      expect(useVersionsStore.getState().error).toBe("Quota exceeded");
    });
  });

  describe("commitTransaction", () => {
    it("posts commit and refreshes list", async () => {
      const postMock = mock(async () => ({
        ...SAMPLE_TRANSACTION,
        status: "committed",
      }));

      const getMock = mock(async () => ({
        transactions: [],
        count: 0,
      }));

      const client = { post: postMock, get: getMock } as unknown as FetchClient;

      await useVersionsStore.getState().commitTransaction("txn-001", client);

      const postPath = postMock.mock.calls[0]![0] as string;
      expect(postPath).toContain("/api/v2/snapshots/txn-001/commit");
      expect(getMock.mock.calls).toHaveLength(1);
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => {
          throw new Error("Already committed");
        }),
        get: mock(async () => ({ transactions: [], count: 0 })),
      } as unknown as FetchClient;

      await useVersionsStore.getState().commitTransaction("txn-001", client);

      expect(useVersionsStore.getState().error).toBe("Already committed");
    });
  });

  describe("rollbackTransaction", () => {
    it("posts rollback and refreshes list", async () => {
      const postMock = mock(async () => ({
        ...SAMPLE_TRANSACTION,
        status: "rolled_back",
      }));

      const getMock = mock(async () => ({
        transactions: [],
        count: 0,
      }));

      const client = { post: postMock, get: getMock } as unknown as FetchClient;

      await useVersionsStore.getState().rollbackTransaction("txn-001", client);

      const postPath = postMock.mock.calls[0]![0] as string;
      expect(postPath).toContain("/api/v2/snapshots/txn-001/rollback");
      expect(getMock.mock.calls).toHaveLength(1);
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => {
          throw new Error("Cannot rollback");
        }),
        get: mock(async () => ({ transactions: [], count: 0 })),
      } as unknown as FetchClient;

      await useVersionsStore.getState().rollbackTransaction("txn-001", client);

      expect(useVersionsStore.getState().error).toBe("Cannot rollback");
    });
  });

  describe("nextStatusFilter", () => {
    it("cycles through statuses", () => {
      expect(nextStatusFilter(null)).toBe("active");
      expect(nextStatusFilter("active")).toBe("committed");
      expect(nextStatusFilter("committed")).toBe("rolled_back");
      expect(nextStatusFilter("rolled_back")).toBe("expired");
      expect(nextStatusFilter("expired")).toBeNull();
    });

    it("returns null (start of cycle) for unknown values", () => {
      // Unknown values get indexOf -1, so (-1+1)%5 = 0, which is null
      expect(nextStatusFilter("unknown")).toBeNull();
    });
  });
});
