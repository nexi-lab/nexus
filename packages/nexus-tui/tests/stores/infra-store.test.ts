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
    delete: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
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
              resource: "/data/important.bin",
              holder: "agent-42",
              status: "held",
              acquired_at: "2025-01-01T10:00:00Z",
              expires_at: "2025-01-01T10:05:00Z",
              ttl_seconds: 300,
            },
            {
              lock_id: "lock-2",
              resource: "/config/settings.json",
              holder: "agent-7",
              status: "expired",
              acquired_at: "2025-01-01T08:00:00Z",
              expires_at: "2025-01-01T08:01:00Z",
              ttl_seconds: 60,
            },
          ],
        },
      });

      await useInfraStore.getState().fetchLocks(client);
      const state = useInfraStore.getState();

      expect(state.locks).toHaveLength(2);
      expect(state.locks[0]!.resource).toBe("/data/important.bin");
      expect(state.locks[0]!.holder).toBe("agent-42");
      expect(state.locks[1]!.status).toBe("expired");
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
    it("updates lock status to released locally", async () => {
      useInfraStore.setState({
        locks: [
          {
            lock_id: "lock-1",
            resource: "/data/file",
            holder: "agent-1",
            status: "held",
            acquired_at: "2025-01-01T00:00:00Z",
            expires_at: "2025-01-01T00:05:00Z",
            ttl_seconds: 300,
          },
        ],
      });

      const client = mockClient({
        "/api/v2/locks/lock-1/release": {},
      });

      await useInfraStore.getState().releaseLock("lock-1", client);
      expect(useInfraStore.getState().locks[0]!.status).toBe("released");
      expect(useInfraStore.getState().error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Lock release denied"); }),
      } as unknown as FetchClient;

      await useInfraStore.getState().releaseLock("lock-1", client);
      expect(useInfraStore.getState().error).toBe("Lock release denied");
    });
  });

  describe("extendLock", () => {
    it("calls extend and refreshes locks", async () => {
      const client = mockClient({
        "/api/v2/locks/lock-1/extend": {},
        "/api/v2/locks": {
          locks: [
            {
              lock_id: "lock-1",
              resource: "/data/file",
              holder: "agent-1",
              status: "held",
              acquired_at: "2025-01-01T00:00:00Z",
              expires_at: "2025-01-01T00:10:00Z",
              ttl_seconds: 600,
            },
          ],
        },
      });

      await useInfraStore.getState().extendLock("lock-1", 600, client);
      const state = useInfraStore.getState();

      expect(state.locks).toHaveLength(1);
      expect(state.locks[0]!.ttl_seconds).toBe(600);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Extension denied"); }),
      } as unknown as FetchClient;

      await useInfraStore.getState().extendLock("lock-1", 300, client);
      expect(useInfraStore.getState().error).toBe("Extension denied");
    });
  });

  describe("fetchSecretAudit", () => {
    it("fetches and stores audit entries", async () => {
      const client = mockClient({
        "/api/v2/secrets/audit": {
          entries: [
            {
              entry_id: "audit-1",
              action: "read",
              secret_name: "DB_PASSWORD",
              actor: "agent-10",
              timestamp: "2025-01-01T12:00:00Z",
              ip_address: "10.0.0.1",
              result: "success",
            },
            {
              entry_id: "audit-2",
              action: "write",
              secret_name: "API_KEY",
              actor: "agent-5",
              timestamp: "2025-01-01T12:05:00Z",
              ip_address: null,
              result: "denied",
            },
          ],
        },
      });

      await useInfraStore.getState().fetchSecretAudit(client);
      const state = useInfraStore.getState();

      expect(state.secretAuditEntries).toHaveLength(2);
      expect(state.secretAuditEntries[0]!.secret_name).toBe("DB_PASSWORD");
      expect(state.secretAuditEntries[0]!.result).toBe("success");
      expect(state.secretAuditEntries[1]!.result).toBe("denied");
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
