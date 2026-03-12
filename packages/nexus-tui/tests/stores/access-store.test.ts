import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useAccessStore } from "../../src/stores/access-store.js";
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
  useAccessStore.setState({
    manifests: [],
    selectedManifestIndex: 0,
    manifestsLoading: false,
    lastPermissionCheck: null,
    permissionCheckLoading: false,
    alerts: [],
    alertsLoading: false,
    leaderboard: [],
    leaderboardLoading: false,
    credentials: [],
    credentialsLoading: false,
    disputes: [],
    disputesLoading: false,
    selectedDisputeIndex: 0,
    activeTab: "manifests",
    error: null,
  });
}

describe("AccessStore", () => {
  beforeEach(() => {
    resetStore();
  });

  describe("setActiveTab", () => {
    it("switches between tabs and clears error", () => {
      useAccessStore.setState({ error: "old error" });
      useAccessStore.getState().setActiveTab("alerts");
      expect(useAccessStore.getState().activeTab).toBe("alerts");
      expect(useAccessStore.getState().error).toBeNull();

      useAccessStore.getState().setActiveTab("reputation");
      expect(useAccessStore.getState().activeTab).toBe("reputation");

      useAccessStore.getState().setActiveTab("credentials");
      expect(useAccessStore.getState().activeTab).toBe("credentials");

      useAccessStore.getState().setActiveTab("manifests");
      expect(useAccessStore.getState().activeTab).toBe("manifests");
    });
  });

  describe("setSelectedManifestIndex", () => {
    it("sets the selected manifest index", () => {
      useAccessStore.getState().setSelectedManifestIndex(3);
      expect(useAccessStore.getState().selectedManifestIndex).toBe(3);
    });
  });

  describe("fetchManifests", () => {
    it("fetches and stores access manifests (list returns summaries without entries)", async () => {
      const client = mockClient({
        "/api/v2/access-manifests": {
          manifests: [
            {
              manifest_id: "m-1",
              agent_id: "agent-alice",
              zone_id: "zone-1",
              name: "read-access",
              status: "active",
              valid_from: "2025-01-01T00:00:00Z",
              valid_until: "2025-12-31T23:59:59Z",
            },
            {
              manifest_id: "m-2",
              agent_id: "agent-bob",
              zone_id: "zone-2",
              name: "write-access",
              status: "expired",
              valid_from: "2025-02-01T00:00:00Z",
              valid_until: "2025-03-01T00:00:00Z",
            },
          ],
          offset: 0,
          limit: 50,
          count: 2,
        },
      });

      await useAccessStore.getState().fetchManifests(client);
      const state = useAccessStore.getState();

      expect(state.manifests).toHaveLength(2);
      expect(state.manifests[0]!.manifest_id).toBe("m-1");
      expect(state.manifests[0]!.agent_id).toBe("agent-alice");
      expect(state.manifests[0]!.zone_id).toBe("zone-1");
      expect(state.manifests[0]!.name).toBe("read-access");
      expect(state.manifests[0]!.entries).toBeUndefined();
      expect(state.manifests[0]!.status).toBe("active");
      expect(state.manifests[1]!.status).toBe("expired");
      expect(state.manifests[1]!.entries).toBeUndefined();
      expect(state.manifestsLoading).toBe(false);
      expect(state.selectedManifestIndex).toBe(0);
      expect(state.error).toBeNull();
    });

    it("calls correct API path with hyphens", async () => {
      const getMock = mock(async () => ({
        manifests: [],
        offset: 0,
        limit: 50,
        count: 0,
      }));
      const client = { get: getMock, post: mock() } as unknown as FetchClient;

      await useAccessStore.getState().fetchManifests(client);
      expect(getMock).toHaveBeenCalledWith("/api/v2/access-manifests");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Access service unavailable");
        }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchManifests(client);
      const state = useAccessStore.getState();
      expect(state.manifestsLoading).toBe(false);
      expect(state.error).toBe("Access service unavailable");
    });

    it("resets selectedManifestIndex on refetch", async () => {
      useAccessStore.setState({ selectedManifestIndex: 5 });
      const client = mockClient({
        "/api/v2/access-manifests": {
          manifests: [],
          offset: 0,
          limit: 50,
          count: 0,
        },
      });

      await useAccessStore.getState().fetchManifests(client);
      expect(useAccessStore.getState().selectedManifestIndex).toBe(0);
    });
  });

  describe("fetchManifestDetail", () => {
    it("fetches single manifest with entries and updates the store", async () => {
      // Pre-populate with summary (no entries)
      useAccessStore.setState({
        manifests: [
          {
            manifest_id: "m-1",
            agent_id: "agent-alice",
            zone_id: "zone-1",
            name: "read-access",
            status: "active",
            valid_from: "2025-01-01T00:00:00Z",
            valid_until: "2025-12-31T23:59:59Z",
          },
        ],
      });

      const client = mockClient({
        "/api/v2/access-manifests/m-1": {
          manifest_id: "m-1",
          agent_id: "agent-alice",
          zone_id: "zone-1",
          name: "read-access",
          entries: [
            {
              tool_pattern: "file:read:*",
              permission: "allow",
              max_calls_per_minute: 100,
            },
          ],
          status: "active",
          valid_from: "2025-01-01T00:00:00Z",
          valid_until: "2025-12-31T23:59:59Z",
        },
      });

      await useAccessStore.getState().fetchManifestDetail("m-1", client);
      const state = useAccessStore.getState();

      expect(state.manifests).toHaveLength(1);
      expect(state.manifests[0]!.entries).toHaveLength(1);
      expect(state.manifests[0]!.entries![0]!.tool_pattern).toBe("file:read:*");
      expect(state.manifests[0]!.entries![0]!.permission).toBe("allow");
      expect(state.manifests[0]!.entries![0]!.max_calls_per_minute).toBe(100);
    });

    it("does not error when manifest is not in store", async () => {
      const client = mockClient({
        "/api/v2/access-manifests/m-unknown": {
          manifest_id: "m-unknown",
          agent_id: "agent-x",
          zone_id: "zone-1",
          name: "unknown",
          entries: [],
          status: "active",
          valid_from: "2025-01-01T00:00:00Z",
          valid_until: "2025-12-31T23:59:59Z",
        },
      });

      await useAccessStore.getState().fetchManifestDetail("m-unknown", client);
      // No crash, store unchanged
      expect(useAccessStore.getState().manifests).toHaveLength(0);
      expect(useAccessStore.getState().error).toBeNull();
    });

    it("silently ignores fetch errors", async () => {
      const client = {
        get: mock(async () => { throw new Error("Not found"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchManifestDetail("m-bad", client);
      // Non-critical: no error set
      expect(useAccessStore.getState().error).toBeNull();
    });
  });

  describe("checkPermission", () => {
    it("evaluates permission and stores result", async () => {
      const client = mockClient({
        "/api/v2/access-manifests/m-1/evaluate": {
          tool_name: "file:read:reports",
          permission: "allow",
          agent_id: "agent-alice",
          manifest_id: "m-1",
        },
      });

      await useAccessStore.getState().checkPermission(
        "m-1",
        "file:read:reports",
        client,
      );
      const state = useAccessStore.getState();

      expect(state.lastPermissionCheck).not.toBeNull();
      expect(state.lastPermissionCheck!.tool_name).toBe("file:read:reports");
      expect(state.lastPermissionCheck!.permission).toBe("allow");
      expect(state.lastPermissionCheck!.agent_id).toBe("agent-alice");
      expect(state.lastPermissionCheck!.manifest_id).toBe("m-1");
      expect(state.permissionCheckLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("stores denied permission evaluation", async () => {
      const client = mockClient({
        "/api/v2/access-manifests/m-2/evaluate": {
          tool_name: "file:delete:critical",
          permission: "deny",
          agent_id: "agent-bob",
          manifest_id: "m-2",
        },
      });

      await useAccessStore.getState().checkPermission(
        "m-2",
        "file:delete:critical",
        client,
      );
      const state = useAccessStore.getState();

      expect(state.lastPermissionCheck!.permission).toBe("deny");
      expect(state.lastPermissionCheck!.tool_name).toBe("file:delete:critical");
    });

    it("posts to correct evaluate endpoint", async () => {
      const postMock = mock(async () => ({
        tool_name: "test",
        permission: "allow",
        agent_id: "a",
        manifest_id: "m-1",
      }));
      const client = {
        get: mock(),
        post: postMock,
      } as unknown as FetchClient;

      await useAccessStore.getState().checkPermission("m-1", "test", client);
      expect(postMock).toHaveBeenCalledWith(
        "/api/v2/access-manifests/m-1/evaluate",
        { tool_name: "test" },
      );
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => {
          throw new Error("Permission evaluation failed");
        }),
      } as unknown as FetchClient;

      await useAccessStore.getState().checkPermission(
        "m-1",
        "file:read:data",
        client,
      );
      const state = useAccessStore.getState();
      expect(state.permissionCheckLoading).toBe(false);
      expect(state.error).toBe("Permission evaluation failed");
    });
  });

  describe("fetchAlerts", () => {
    it("fetches and stores governance alerts", async () => {
      const client = mockClient({
        "/api/v2/governance/alerts": {
          alerts: [
            {
              alert_id: "a-1",
              severity: "critical",
              category: "access_violation",
              message: "Unauthorized access attempt detected",
              agent_id: "agent-rogue",
              created_at: "2025-01-15T10:00:00Z",
              resolved: false,
            },
            {
              alert_id: "a-2",
              severity: "info",
              category: "audit",
              message: "New manifest created",
              agent_id: null,
              created_at: "2025-01-15T09:00:00Z",
              resolved: true,
            },
          ],
        },
      });

      await useAccessStore.getState().fetchAlerts(client);
      const state = useAccessStore.getState();

      expect(state.alerts).toHaveLength(2);
      expect(state.alerts[0]!.alert_id).toBe("a-1");
      expect(state.alerts[0]!.severity).toBe("critical");
      expect(state.alerts[0]!.resolved).toBe(false);
      expect(state.alerts[1]!.agent_id).toBeNull();
      expect(state.alerts[1]!.resolved).toBe(true);
      expect(state.alertsLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Governance service down");
        }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchAlerts(client);
      const state = useAccessStore.getState();
      expect(state.alertsLoading).toBe(false);
      expect(state.error).toBe("Governance service down");
    });
  });

  describe("fetchLeaderboard", () => {
    it("fetches and stores leaderboard entries from reputation endpoint", async () => {
      const client = mockClient({
        "/api/v2/reputation/leaderboard": {
          entries: [
            {
              agent_id: "agent-alice",
              context: "default",
              window: "30d",
              composite_score: 0.92,
              composite_confidence: 0.85,
              total_interactions: 150,
              positive_interactions: 140,
              negative_interactions: 10,
              global_trust_score: 0.88,
              zone_id: "zone-1",
              updated_at: "2025-01-15T12:00:00Z",
            },
            {
              agent_id: "agent-bob",
              context: "default",
              window: "30d",
              composite_score: 0.45,
              composite_confidence: 0.60,
              total_interactions: 50,
              positive_interactions: 25,
              negative_interactions: 25,
              global_trust_score: null,
              zone_id: "zone-2",
              updated_at: "2025-01-14T08:00:00Z",
            },
          ],
        },
      });

      await useAccessStore.getState().fetchLeaderboard(client);
      const state = useAccessStore.getState();

      expect(state.leaderboard).toHaveLength(2);
      expect(state.leaderboard[0]!.agent_id).toBe("agent-alice");
      expect(state.leaderboard[0]!.composite_score).toBe(0.92);
      expect(state.leaderboard[0]!.composite_confidence).toBe(0.85);
      expect(state.leaderboard[0]!.total_interactions).toBe(150);
      expect(state.leaderboard[0]!.positive_interactions).toBe(140);
      expect(state.leaderboard[0]!.negative_interactions).toBe(10);
      expect(state.leaderboard[0]!.global_trust_score).toBe(0.88);
      expect(state.leaderboard[0]!.zone_id).toBe("zone-1");
      expect(state.leaderboard[1]!.global_trust_score).toBeNull();
      expect(state.leaderboardLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("calls correct reputation leaderboard path", async () => {
      const getMock = mock(async () => ({ entries: [] }));
      const client = { get: getMock, post: mock() } as unknown as FetchClient;

      await useAccessStore.getState().fetchLeaderboard(client);
      expect(getMock).toHaveBeenCalledWith("/api/v2/reputation/leaderboard");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Leaderboard unavailable");
        }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchLeaderboard(client);
      const state = useAccessStore.getState();
      expect(state.leaderboardLoading).toBe(false);
      expect(state.error).toBe("Leaderboard unavailable");
    });
  });

  describe("fetchCredentials", () => {
    it("fetches credentials for a specific agent", async () => {
      const client = mockClient({
        "/api/v2/agents/agent-alice/credentials": {
          agent_id: "agent-alice",
          count: 2,
          credentials: [
            {
              credential_id: "cred-1",
              issuer_did: "did:nexus:ca-1",
              subject_did: "did:nexus:agent-alice",
              subject_agent_id: "agent-alice",
              is_active: true,
              created_at: "2025-01-01T00:00:00Z",
              expires_at: "2025-12-31T23:59:59Z",
              revoked_at: null,
              delegation_depth: 0,
            },
            {
              credential_id: "cred-2",
              issuer_did: "did:nexus:ca-2",
              subject_did: "did:nexus:agent-alice",
              subject_agent_id: "agent-alice",
              is_active: false,
              created_at: "2024-06-01T00:00:00Z",
              expires_at: "2024-12-31T23:59:59Z",
              revoked_at: "2024-10-15T00:00:00Z",
              delegation_depth: 1,
            },
          ],
        },
      });

      await useAccessStore.getState().fetchCredentials("agent-alice", client);
      const state = useAccessStore.getState();

      expect(state.credentials).toHaveLength(2);
      expect(state.credentials[0]!.credential_id).toBe("cred-1");
      expect(state.credentials[0]!.issuer_did).toBe("did:nexus:ca-1");
      expect(state.credentials[0]!.subject_did).toBe("did:nexus:agent-alice");
      expect(state.credentials[0]!.subject_agent_id).toBe("agent-alice");
      expect(state.credentials[0]!.is_active).toBe(true);
      expect(state.credentials[0]!.revoked_at).toBeNull();
      expect(state.credentials[0]!.delegation_depth).toBe(0);
      expect(state.credentials[1]!.is_active).toBe(false);
      expect(state.credentials[1]!.revoked_at).toBe("2024-10-15T00:00:00Z");
      expect(state.credentials[1]!.delegation_depth).toBe(1);
      expect(state.credentialsLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("calls agent-specific credentials endpoint", async () => {
      const getMock = mock(async () => ({
        agent_id: "agent-bob",
        count: 0,
        credentials: [],
      }));
      const client = { get: getMock, post: mock() } as unknown as FetchClient;

      await useAccessStore.getState().fetchCredentials("agent-bob", client);
      expect(getMock).toHaveBeenCalledWith(
        "/api/v2/agents/agent-bob/credentials",
      );
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Credentials service down");
        }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchCredentials("agent-alice", client);
      const state = useAccessStore.getState();
      expect(state.credentialsLoading).toBe(false);
      expect(state.error).toBe("Credentials service down");
    });
  });

  describe("fetchDispute", () => {
    it("fetches a single dispute and adds it to the list", async () => {
      const client = mockClient({
        "/api/v2/disputes/d-1": {
          id: "d-1",
          exchange_id: "ex-1",
          zone_id: "zone-1",
          complainant_agent_id: "agent-a",
          respondent_agent_id: "agent-b",
          status: "open",
          tier: 1,
          reason: "Service not delivered",
          resolution: null,
          resolution_evidence_hash: null,
          escrow_amount: "100.00",
          escrow_released: false,
          filed_at: "2025-06-01T12:00:00Z",
          resolved_at: null,
          appeal_deadline: "2025-06-08T12:00:00Z",
        },
      });

      await useAccessStore.getState().fetchDispute("d-1", client);
      const state = useAccessStore.getState();

      expect(state.disputes).toHaveLength(1);
      expect(state.disputes[0]!.id).toBe("d-1");
      expect(state.disputes[0]!.status).toBe("open");
      expect(state.disputes[0]!.reason).toBe("Service not delivered");
      expect(state.disputes[0]!.escrow_amount).toBe("100.00");
      expect(state.disputesLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Dispute not found"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchDispute("d-999", client);
      expect(useAccessStore.getState().error).toBe("Dispute not found");
    });
  });

  describe("fileDispute", () => {
    it("files a dispute via POST and adds to list", async () => {
      const client = mockClient({
        "/api/v2/exchanges/ex-1/dispute": {
          id: "d-new",
          exchange_id: "ex-1",
          zone_id: "zone-1",
          complainant_agent_id: "agent-a",
          respondent_agent_id: "agent-b",
          status: "open",
          tier: 1,
          reason: "Bad quality",
          resolution: null,
          resolution_evidence_hash: null,
          escrow_amount: null,
          escrow_released: false,
          filed_at: "2025-06-01T12:00:00Z",
          resolved_at: null,
          appeal_deadline: null,
        },
      });

      await useAccessStore.getState().fileDispute(
        "ex-1", "agent-a", "agent-b", "Bad quality", client,
      );
      const state = useAccessStore.getState();

      expect(state.disputes).toHaveLength(1);
      expect(state.disputes[0]!.id).toBe("d-new");
      expect(state.disputesLoading).toBe(false);
    });

    it("sets error on duplicate dispute", async () => {
      const client = {
        post: mock(async () => { throw new Error("Dispute already filed"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fileDispute(
        "ex-1", "a", "b", "reason", client,
      );
      expect(useAccessStore.getState().error).toBe("Dispute already filed");
    });
  });

  describe("resolveDispute", () => {
    it("resolves a dispute and updates local state", async () => {
      useAccessStore.setState({
        disputes: [{
          id: "d-1",
          exchange_id: "ex-1",
          zone_id: "zone-1",
          complainant_agent_id: "agent-a",
          respondent_agent_id: "agent-b",
          status: "open",
          tier: 1,
          reason: "Service not delivered",
          resolution: null,
          resolution_evidence_hash: null,
          escrow_amount: "100.00",
          escrow_released: false,
          filed_at: "2025-06-01T12:00:00Z",
          resolved_at: null,
          appeal_deadline: null,
        }],
      });

      const client = mockClient({
        "/api/v2/disputes/d-1/resolve": {
          id: "d-1",
          exchange_id: "ex-1",
          zone_id: "zone-1",
          complainant_agent_id: "agent-a",
          respondent_agent_id: "agent-b",
          status: "resolved",
          tier: 1,
          reason: "Service not delivered",
          resolution: "Refund issued",
          resolution_evidence_hash: "hash123",
          escrow_amount: "100.00",
          escrow_released: true,
          filed_at: "2025-06-01T12:00:00Z",
          resolved_at: "2025-06-05T10:00:00Z",
          appeal_deadline: null,
        },
      });

      await useAccessStore.getState().resolveDispute("d-1", "Refund issued", client);
      const state = useAccessStore.getState();

      expect(state.disputes[0]!.status).toBe("resolved");
      expect(state.disputes[0]!.resolution).toBe("Refund issued");
      expect(state.disputes[0]!.escrow_released).toBe(true);
      expect(state.disputesLoading).toBe(false);
    });
  });

  describe("setSelectedDisputeIndex", () => {
    it("sets the selected dispute index", () => {
      useAccessStore.getState().setSelectedDisputeIndex(2);
      expect(useAccessStore.getState().selectedDisputeIndex).toBe(2);
    });
  });

  describe("error handling", () => {
    it("clears error when switching tabs", () => {
      useAccessStore.setState({ error: "previous error" });
      useAccessStore.getState().setActiveTab("alerts");
      expect(useAccessStore.getState().error).toBeNull();
    });

    it("fetchManifests clears previous error on success", async () => {
      useAccessStore.setState({ error: "old error" });

      const client = mockClient({
        "/api/v2/access-manifests": {
          manifests: [],
          offset: 0,
          limit: 50,
          count: 0,
        },
      });

      await useAccessStore.getState().fetchManifests(client);
      expect(useAccessStore.getState().error).toBeNull();
    });
  });
});
