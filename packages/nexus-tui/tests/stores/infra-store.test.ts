import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useInfraStore } from "../../src/stores/infra-store.js";
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
  useInfraStore.setState({
    connectors: [],
    selectedConnectorIndex: 0,
    connectorsLoading: false,
    subscriptions: [],
    selectedSubscriptionIndex: 0,
    subscriptionsLoading: false,
    locks: [],
    selectedLockIndex: 0,
    locksLoading: false,
    secretAuditEntries: [],
    secretsLoading: false,
    activeTab: "connectors",
    error: null,
  });
}

describe("InfraStore", () => {
  beforeEach(() => {
    resetStore();
  });

  describe("setActiveTab", () => {
    it("switches between tabs and clears error", () => {
      useInfraStore.setState({ error: "old error" });
      useInfraStore.getState().setActiveTab("locks");
      expect(useInfraStore.getState().activeTab).toBe("locks");
      expect(useInfraStore.getState().error).toBeNull();
    });
  });

  describe("setSelectedConnectorIndex", () => {
    it("sets the selected connector index", () => {
      useInfraStore.getState().setSelectedConnectorIndex(3);
      expect(useInfraStore.getState().selectedConnectorIndex).toBe(3);
    });
  });

  describe("setSelectedSubscriptionIndex", () => {
    it("sets the selected subscription index", () => {
      useInfraStore.getState().setSelectedSubscriptionIndex(5);
      expect(useInfraStore.getState().selectedSubscriptionIndex).toBe(5);
    });
  });

  describe("setSelectedLockIndex", () => {
    it("sets the selected lock index", () => {
      useInfraStore.getState().setSelectedLockIndex(2);
      expect(useInfraStore.getState().selectedLockIndex).toBe(2);
    });
  });

  describe("fetchConnectors", () => {
    it("fetches and stores connectors", async () => {
      const client = mockClient({
        "/api/v2/connectors": {
          connectors: [
            {
              connector_id: "conn-1",
              name: "S3 Connector",
              type: "storage",
              status: "active",
              capabilities: ["read", "write", "list"],
              config: { bucket: "my-bucket" },
              created_at: "2025-01-01T00:00:00Z",
              last_seen: "2025-01-02T12:00:00Z",
            },
            {
              connector_id: "conn-2",
              name: "GCS Connector",
              type: "storage",
              status: "inactive",
              capabilities: ["read"],
              config: {},
              created_at: "2025-01-01T00:00:00Z",
              last_seen: null,
            },
          ],
        },
      });

      await useInfraStore.getState().fetchConnectors(client);
      const state = useInfraStore.getState();

      expect(state.connectors).toHaveLength(2);
      expect(state.connectors[0]!.name).toBe("S3 Connector");
      expect(state.connectors[0]!.capabilities).toEqual(["read", "write", "list"]);
      expect(state.connectors[1]!.status).toBe("inactive");
      expect(state.connectorsLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Connectors unavailable"); }),
      } as unknown as FetchClient;

      await useInfraStore.getState().fetchConnectors(client);
      expect(useInfraStore.getState().connectorsLoading).toBe(false);
      expect(useInfraStore.getState().error).toBe("Connectors unavailable");
    });
  });

  describe("fetchSubscriptions", () => {
    it("fetches and stores subscriptions", async () => {
      const client = mockClient({
        "/api/v2/subscriptions": {
          subscriptions: [
            {
              subscription_id: "sub-1",
              event_type: "file.write",
              endpoint: "https://hooks.example.com/notify",
              status: "active",
              filter: null,
              created_at: "2025-01-01T00:00:00Z",
              last_triggered: "2025-01-02T10:00:00Z",
              trigger_count: 42,
            },
          ],
        },
      });

      await useInfraStore.getState().fetchSubscriptions(client);
      const state = useInfraStore.getState();

      expect(state.subscriptions).toHaveLength(1);
      expect(state.subscriptions[0]!.event_type).toBe("file.write");
      expect(state.subscriptions[0]!.trigger_count).toBe(42);
      expect(state.subscriptionsLoading).toBe(false);
      expect(state.selectedSubscriptionIndex).toBe(0);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Subscriptions down"); }),
      } as unknown as FetchClient;

      await useInfraStore.getState().fetchSubscriptions(client);
      expect(useInfraStore.getState().error).toBe("Subscriptions down");
    });
  });

  describe("deleteSubscription", () => {
    it("removes subscription from local list", async () => {
      useInfraStore.setState({
        subscriptions: [
          {
            subscription_id: "sub-1",
            event_type: "file.write",
            endpoint: "https://example.com",
            status: "active",
            filter: null,
            created_at: "2025-01-01T00:00:00Z",
            last_triggered: null,
            trigger_count: 0,
          },
          {
            subscription_id: "sub-2",
            event_type: "file.delete",
            endpoint: "https://example.com",
            status: "active",
            filter: null,
            created_at: "2025-01-01T00:00:00Z",
            last_triggered: null,
            trigger_count: 0,
          },
        ],
      });

      const client = mockClient({
        "/api/v2/subscriptions/sub-1": {},
      });

      await useInfraStore.getState().deleteSubscription("sub-1", client);
      const state = useInfraStore.getState();

      expect(state.subscriptions).toHaveLength(1);
      expect(state.subscriptions[0]!.subscription_id).toBe("sub-2");
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        delete: mock(async () => { throw new Error("Not allowed"); }),
      } as unknown as FetchClient;

      await useInfraStore.getState().deleteSubscription("sub-1", client);
      expect(useInfraStore.getState().error).toBe("Not allowed");
    });
  });

  describe("testSubscription", () => {
    it("calls test endpoint without error", async () => {
      const client = mockClient({
        "/api/v2/subscriptions/sub-1/test": { ok: true },
      });

      await useInfraStore.getState().testSubscription("sub-1", client);
      expect(useInfraStore.getState().error).toBeNull();
      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Test failed"); }),
      } as unknown as FetchClient;

      await useInfraStore.getState().testSubscription("sub-1", client);
      expect(useInfraStore.getState().error).toBe("Test failed");
    });
  });

  describe("fetchLocks", () => {
    it("fetches and stores locks", async () => {
      const client = mockClient({
        "/api/v2/locks": {
          locks: [
            {
              lock_id: "lock-1",
              mode: "mutex",
              max_holders: 1,
              holder_info: "agent-42",
              acquired_at: 1704067200,
              expires_at: 1704067500,
              fence_token: 1,
              resource: "/data/important.bin",
            },
            {
              lock_id: "lock-2",
              mode: "mutex",
              max_holders: 1,
              holder_info: "agent-7",
              acquired_at: 1704060000,
              expires_at: 1704060060,
              fence_token: 2,
              resource: "/config/settings.json",
            },
          ],
          count: 2,
        },
      });

      await useInfraStore.getState().fetchLocks(client);
      const state = useInfraStore.getState();

      expect(state.locks).toHaveLength(2);
      expect(state.locks[0]!.resource).toBe("/data/important.bin");
      expect(state.locks[0]!.holder_info).toBe("agent-42");
      expect(state.locks[0]!.mode).toBe("mutex");
      expect(state.locks[0]!.fence_token).toBe(1);
      expect(state.locks[1]!.lock_id).toBe("lock-2");
      expect(state.locksLoading).toBe(false);
      expect(state.selectedLockIndex).toBe(0);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Locks unavailable"); }),
      } as unknown as FetchClient;

      await useInfraStore.getState().fetchLocks(client);
      expect(useInfraStore.getState().error).toBe("Locks unavailable");
    });
  });

  describe("releaseLock", () => {
    it("calls DELETE and removes lock from local list", async () => {
      useInfraStore.setState({
        locks: [
          {
            lock_id: "lock-1",
            mode: "mutex",
            max_holders: 1,
            holder_info: "agent-1",
            acquired_at: 1704067200,
            expires_at: 1704067500,
            fence_token: 1,
            resource: "/data/file",
          },
        ],
      });

      const client = mockClient({
        "/api/v2/locks/": {},
      });

      await useInfraStore.getState().releaseLock("/data/file", "lock-1", client);
      expect(useInfraStore.getState().locks).toHaveLength(0);
      expect(useInfraStore.getState().error).toBeNull();
      expect((client.deleteNoContent as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    });

    it("sets error on failure", async () => {
      const client = {
        deleteNoContent: mock(async () => { throw new Error("Lock release denied"); }),
      } as unknown as FetchClient;

      await useInfraStore.getState().releaseLock("/data/file", "lock-1", client);
      expect(useInfraStore.getState().error).toBe("Lock release denied");
    });
  });

  describe("extendLock", () => {
    it("calls PATCH and refreshes locks", async () => {
      const lockData = {
        locks: [
          {
            lock_id: "lock-1",
            mode: "mutex",
            max_holders: 1,
            holder_info: "agent-1",
            acquired_at: 1704067200,
            expires_at: 1704067800,
            fence_token: 1,
            resource: "/data/file",
          },
        ],
        count: 1,
      };
      const client = mockClient({
        "/api/v2/locks": lockData,
      });

      await useInfraStore.getState().extendLock("/data/file", "lock-1", 600, client);
      const state = useInfraStore.getState();

      expect(state.locks).toHaveLength(1);
      expect(state.locks[0]!.expires_at).toBe(1704067800);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        patch: mock(async () => { throw new Error("Extension denied"); }),
      } as unknown as FetchClient;

      await useInfraStore.getState().extendLock("/data/file", "lock-x", 300, client);
      expect(useInfraStore.getState().error).toBe("Extension denied");
    });
  });

  describe("fetchSecretAudit", () => {
    it("fetches and stores audit events", async () => {
      const client = mockClient({
        "/api/v2/secrets-audit/events": {
          events: [
            {
              id: "audit-1",
              record_hash: "abc123",
              created_at: "2025-01-01T12:00:00Z",
              event_type: "read",
              actor_id: "agent-10",
              provider: null,
              credential_id: null,
              token_family_id: null,
              zone_id: "zone-1",
              ip_address: "10.0.0.1",
              details: null,
              metadata_hash: null,
            },
            {
              id: "audit-2",
              record_hash: "def456",
              created_at: "2025-01-01T12:05:00Z",
              event_type: "write",
              actor_id: "agent-5",
              provider: "vault",
              credential_id: "cred-1",
              token_family_id: null,
              zone_id: "zone-1",
              ip_address: null,
              details: "Updated secret",
              metadata_hash: "hash789",
            },
          ],
          limit: 50,
          has_more: false,
          total: 2,
          next_cursor: null,
        },
      });

      await useInfraStore.getState().fetchSecretAudit(client);
      const state = useInfraStore.getState();

      expect(state.secretAuditEntries).toHaveLength(2);
      expect(state.secretAuditEntries[0]!.id).toBe("audit-1");
      expect(state.secretAuditEntries[0]!.event_type).toBe("read");
      expect(state.secretAuditEntries[0]!.actor_id).toBe("agent-10");
      expect(state.secretAuditEntries[0]!.ip_address).toBe("10.0.0.1");
      expect(state.secretAuditEntries[1]!.provider).toBe("vault");
      expect(state.secretAuditEntries[1]!.details).toBe("Updated secret");
      expect(state.secretsLoading).toBe(false);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Audit unavailable"); }),
      } as unknown as FetchClient;

      await useInfraStore.getState().fetchSecretAudit(client);
      expect(useInfraStore.getState().error).toBe("Audit unavailable");
    });
  });

  describe("createSubscription", () => {
    it("calls POST and refreshes subscriptions", async () => {
      const client = mockClient({
        "/api/v2/subscriptions": {
          subscriptions: [
            {
              subscription_id: "sub-new",
              event_type: "agent.ready",
              endpoint: "https://example.com/hook",
              status: "active",
              filter: null,
              created_at: "2025-01-01T00:00:00Z",
              last_triggered: null,
              trigger_count: 0,
            },
          ],
        },
      });

      await useInfraStore.getState().createSubscription("agent.ready", "https://example.com/hook", client);
      const state = useInfraStore.getState();

      expect(state.subscriptions).toHaveLength(1);
      expect(state.subscriptions[0]!.event_type).toBe("agent.ready");
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Creation failed"); }),
      } as unknown as FetchClient;

      await useInfraStore.getState().createSubscription("test", "http://bad", client);
      expect(useInfraStore.getState().error).toBe("Creation failed");
    });
  });

  describe("error handling", () => {
    it("handles non-Error thrown objects", async () => {
      const client = {
        get: mock(async () => { throw "string error"; }),
      } as unknown as FetchClient;

      await useInfraStore.getState().fetchConnectors(client);
      expect(useInfraStore.getState().error).toBe("Failed to fetch connectors");
    });
  });
});
