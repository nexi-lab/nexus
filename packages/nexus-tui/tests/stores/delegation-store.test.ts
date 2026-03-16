/**
 * Tests for the shared delegation store (Decision 5A, 12A).
 *
 * Covers:
 * - fetchDelegations with pagination and status filter
 * - createDelegation with auto-refresh
 * - revokeDelegation with optimistic update
 * - completeDelegation with outcome tracking
 * - fetchDelegationChain
 * - fetchNamespaceDetail and updateNamespaceConfig
 * - Error handling for all actions
 */

import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useDelegationStore } from "../../src/stores/delegation-store.js";
import type { FetchClient } from "@nexus/api-client";

// =============================================================================
// Helpers
// =============================================================================

function mockClient(responses: Record<string, unknown>): FetchClient {
  const handler = mock(async (path: string) => {
    for (const [pattern, response] of Object.entries(responses)) {
      if (path.includes(pattern)) return response;
    }
    throw new Error(`Unmocked path: ${path}`);
  });
  return {
    get: mock(async (path: string) => handler(path)),
    post: mock(async (path: string, _body?: unknown) => handler(path)),
    delete: mock(async (path: string) => handler(path)),
    patch: mock(async (path: string, _body?: unknown) => handler(path)),
  } as unknown as FetchClient;
}

function mockErrorClient(errorMessage: string): FetchClient {
  const throwError = mock(async () => {
    throw new Error(errorMessage);
  });
  return {
    get: throwError,
    post: throwError,
    delete: throwError,
    patch: throwError,
  } as unknown as FetchClient;
}

const MOCK_DELEGATION = {
  delegation_id: "del-001",
  agent_id: "worker-1",
  parent_agent_id: "coordinator-1",
  delegation_mode: "copy",
  status: "active",
  scope_prefix: "/data",
  lease_expires_at: null,
  zone_id: "root",
  intent: "process data",
  depth: 0,
  can_sub_delegate: false,
  created_at: "2026-01-01T00:00:00Z",
};

function resetStore(): void {
  useDelegationStore.setState({
    delegations: [],
    delegationsLoading: false,
    selectedDelegationIndex: 0,
    lastDelegationCreate: null,
    delegationChain: null,
    delegationChainLoading: false,
    namespaceDetail: null,
    namespaceDetailLoading: false,
    error: null,
    delegationsTotal: 0,
    delegationsLimit: 50,
    delegationsOffset: 0,
    delegationsStatusFilter: null,
  });
}

// =============================================================================
// Tests
// =============================================================================

describe("DelegationStore", () => {
  beforeEach(() => {
    resetStore();
  });

  describe("fetchDelegations", () => {
    it("fetches delegations with default pagination", async () => {
      const client = mockClient({
        "/api/v2/agents/delegate": {
          delegations: [MOCK_DELEGATION],
          total: 1,
          limit: 50,
          offset: 0,
        },
      });

      await useDelegationStore.getState().fetchDelegations(client);
      const state = useDelegationStore.getState();

      expect(state.delegations).toHaveLength(1);
      expect(state.delegations[0].delegation_id).toBe("del-001");
      expect(state.delegationsTotal).toBe(1);
      expect(state.delegationsLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("respects pagination options", async () => {
      const client = mockClient({
        "limit=10&offset=5": {
          delegations: [],
          total: 20,
          limit: 10,
          offset: 5,
        },
      });

      await useDelegationStore.getState().fetchDelegations(client, {
        limit: 10,
        offset: 5,
      });
      const state = useDelegationStore.getState();

      expect(state.delegationsLimit).toBe(10);
      expect(state.delegationsOffset).toBe(5);
      expect(state.delegationsTotal).toBe(20);
    });

    it("applies status filter", async () => {
      const client = mockClient({
        "status=revoked": {
          delegations: [{ ...MOCK_DELEGATION, status: "revoked" }],
          total: 1,
          limit: 50,
          offset: 0,
        },
      });

      await useDelegationStore.getState().fetchDelegations(client, {
        status: "revoked",
      });
      const state = useDelegationStore.getState();

      expect(state.delegationsStatusFilter).toBe("revoked");
      expect(state.delegations[0].status).toBe("revoked");
    });

    it("sets error on fetch failure", async () => {
      const client = mockErrorClient("Network error");

      await useDelegationStore.getState().fetchDelegations(client);
      const state = useDelegationStore.getState();

      expect(state.delegationsLoading).toBe(false);
      expect(state.error).toBe("Network error");
    });
  });

  describe("createDelegation", () => {
    it("creates delegation and stores response", async () => {
      const createResponse = {
        delegation_id: "del-new",
        worker_agent_id: "worker-new",
        api_key: "key-123",
        mount_table: ["/data"],
        expires_at: null,
        delegation_mode: "copy",
      };

      const client = mockClient({
        "/api/v2/agents/delegate": createResponse,
      });
      // Override get for the re-fetch
      (client.get as ReturnType<typeof mock>) = mock(async () => ({
        delegations: [MOCK_DELEGATION],
        total: 1,
      }));

      await useDelegationStore.getState().createDelegation(
        {
          worker_id: "worker-new",
          worker_name: "New Worker",
          namespace_mode: "copy",
          intent: "test",
          can_sub_delegate: false,
        },
        client,
      );
      const state = useDelegationStore.getState();

      expect(state.lastDelegationCreate?.delegation_id).toBe("del-new");
      expect(state.delegationsLoading).toBe(false);
    });

    it("sets error on creation failure", async () => {
      const client = mockErrorClient("Insufficient trust");

      await useDelegationStore.getState().createDelegation(
        {
          worker_id: "w",
          worker_name: "W",
          namespace_mode: "copy",
          intent: "",
          can_sub_delegate: false,
        },
        client,
      );
      const state = useDelegationStore.getState();

      expect(state.error).toBe("Insufficient trust");
      expect(state.delegationsLoading).toBe(false);
    });
  });

  describe("revokeDelegation", () => {
    it("optimistically updates status to revoked", async () => {
      useDelegationStore.setState({
        delegations: [MOCK_DELEGATION],
      });

      const client = mockClient({
        "del-001": { status: "revoked", delegation_id: "del-001" },
      });

      await useDelegationStore.getState().revokeDelegation("del-001", client);
      const state = useDelegationStore.getState();

      expect(state.delegations[0].status).toBe("revoked");
      expect(state.delegationsLoading).toBe(false);
    });

    it("sets error on revoke failure", async () => {
      useDelegationStore.setState({
        delegations: [MOCK_DELEGATION],
      });

      const client = mockErrorClient("Only parent can revoke");

      await useDelegationStore.getState().revokeDelegation("del-001", client);
      const state = useDelegationStore.getState();

      expect(state.error).toBe("Only parent can revoke");
    });
  });

  describe("completeDelegation", () => {
    it("updates delegation status to outcome", async () => {
      useDelegationStore.setState({
        delegations: [MOCK_DELEGATION],
      });

      const client = mockClient({
        "del-001/complete": {
          status: "completed",
          delegation_id: "del-001",
          outcome: "completed",
        },
      });

      await useDelegationStore.getState().completeDelegation(
        "del-001",
        "completed",
        0.9,
        client,
      );
      const state = useDelegationStore.getState();

      expect(state.delegations[0].status).toBe("completed");
    });
  });

  describe("fetchDelegationChain", () => {
    it("fetches chain and stores result", async () => {
      const chain = {
        chain: [
          {
            delegation_id: "del-001",
            agent_id: "worker-1",
            parent_agent_id: "coordinator-1",
            delegation_mode: "copy",
            status: "active",
            depth: 0,
            intent: "test",
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        total_depth: 0,
      };

      const client = mockClient({
        "del-001/chain": chain,
      });

      await useDelegationStore.getState().fetchDelegationChain("del-001", client);
      const state = useDelegationStore.getState();

      expect(state.delegationChain?.chain).toHaveLength(1);
      expect(state.delegationChain?.total_depth).toBe(0);
      expect(state.delegationChainLoading).toBe(false);
    });

    it("sets error on chain fetch failure", async () => {
      const client = mockErrorClient("Not found");

      await useDelegationStore.getState().fetchDelegationChain("del-xxx", client);
      const state = useDelegationStore.getState();

      expect(state.error).toBe("Not found");
      expect(state.delegationChainLoading).toBe(false);
    });
  });

  describe("fetchNamespaceDetail", () => {
    it("fetches namespace detail", async () => {
      const nsDetail = {
        delegation_id: "del-001",
        agent_id: "worker-1",
        delegation_mode: "copy",
        scope_prefix: "/data",
        removed_grants: ["/secret"],
        added_grants: ["/public"],
        readonly_paths: ["/readonly"],
        zone_id: "root",
      };

      const client = mockClient({
        "del-001/namespace": nsDetail,
      });

      await useDelegationStore.getState().fetchNamespaceDetail("del-001", client);
      const state = useDelegationStore.getState();

      expect(state.namespaceDetail?.scope_prefix).toBe("/data");
      expect(state.namespaceDetail?.removed_grants).toEqual(["/secret"]);
      expect(state.namespaceDetailLoading).toBe(false);
    });
  });

  describe("updateNamespaceConfig", () => {
    it("updates namespace config", async () => {
      const updated = {
        delegation_id: "del-001",
        agent_id: "worker-1",
        delegation_mode: "copy",
        scope_prefix: "/data/subset",
        removed_grants: ["/secret", "/private"],
        added_grants: [],
        readonly_paths: [],
        zone_id: "root",
      };

      const client = mockClient({
        "del-001/namespace": updated,
      });

      await useDelegationStore.getState().updateNamespaceConfig(
        "del-001",
        { scope_prefix: "/data/subset", remove_grants: ["/secret", "/private"] },
        client,
      );
      const state = useDelegationStore.getState();

      expect(state.namespaceDetail?.scope_prefix).toBe("/data/subset");
      expect(state.namespaceDetailLoading).toBe(false);
    });

    it("sets error on update failure", async () => {
      const client = mockErrorClient("Escalation error");

      await useDelegationStore.getState().updateNamespaceConfig(
        "del-001",
        { scope_prefix: "/" },
        client,
      );
      const state = useDelegationStore.getState();

      expect(state.error).toBe("Escalation error");
      expect(state.namespaceDetailLoading).toBe(false);
    });
  });

  describe("setStatusFilter", () => {
    it("sets the status filter", () => {
      useDelegationStore.getState().setStatusFilter("active");
      expect(useDelegationStore.getState().delegationsStatusFilter).toBe("active");
    });

    it("clears the status filter with null", () => {
      useDelegationStore.getState().setStatusFilter("active");
      useDelegationStore.getState().setStatusFilter(null);
      expect(useDelegationStore.getState().delegationsStatusFilter).toBeNull();
    });
  });

  describe("setSelectedDelegationIndex", () => {
    it("updates the selected index", () => {
      useDelegationStore.getState().setSelectedDelegationIndex(3);
      expect(useDelegationStore.getState().selectedDelegationIndex).toBe(3);
    });
  });
});
