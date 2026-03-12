import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useAccessStore } from "../../src/stores/access-store.js";
import type { FetchClient } from "@nexus/api-client";

function mockClient(responses: Record<string, unknown>): FetchClient {
  const handler = mock(async (path: string) => {
    for (const [pattern, response] of Object.entries(responses)) {
      if (path.includes(pattern)) return response;
    }
    throw new Error(`Unmocked path: ${path}`);
  });
  return {
    get: mock(async (path: string) => handler(path)),
    post: mock(async (path: string) => handler(path)),
    delete: mock(async (path: string) => handler(path)),
    patch: mock(async (path: string) => handler(path)),
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
    selectedAlertIndex: 0,
    leaderboard: [],
    leaderboardLoading: false,
    credentials: [],
    credentialsLoading: false,
    disputes: [],
    disputesLoading: false,
    selectedDisputeIndex: 0,
    fraudScores: [],
    fraudScoresLoading: false,
    selectedFraudIndex: 0,
    delegations: [],
    delegationsLoading: false,
    selectedDelegationIndex: 0,
    lastDelegationCreate: null,
    delegationChain: null,
    delegationChainLoading: false,
    governanceCheck: null,
    governanceCheckLoading: false,
    namespaceDetail: null,
    namespaceDetailLoading: false,
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
    it("fetches and stores governance alerts with zone_id", async () => {
      const client = mockClient({
        "/api/v2/governance/alerts": {
          alerts: [
            {
              alert_id: "a-1",
              agent_id: "agent-rogue",
              zone_id: "zone-1",
              severity: "critical",
              alert_type: "access_violation",
              details: { reason: "Unauthorized access attempt detected" },
              resolved: false,
              created_at: "2025-01-15T10:00:00Z",
            },
            {
              alert_id: "a-2",
              agent_id: null,
              zone_id: "zone-1",
              severity: "info",
              alert_type: "audit",
              details: "New manifest created",
              resolved: true,
              created_at: "2025-01-15T09:00:00Z",
            },
          ],
        },
      });

      await useAccessStore.getState().fetchAlerts("zone-1", client);
      const state = useAccessStore.getState();

      expect(state.alerts).toHaveLength(2);
      expect(state.alerts[0]!.alert_id).toBe("a-1");
      expect(state.alerts[0]!.severity).toBe("critical");
      expect(state.alerts[0]!.alert_type).toBe("access_violation");
      expect(state.alerts[0]!.resolved).toBe(false);
      expect(state.alerts[1]!.agent_id).toBeNull();
      expect(state.alerts[1]!.resolved).toBe(true);
      expect(state.alertsLoading).toBe(false);
      expect(state.error).toBeNull();

      // Verify zone_id passed as query param
      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("zone_id=zone-1");
    });

    it("fetches without zone_id when undefined", async () => {
      const client = mockClient({
        "/api/v2/governance/alerts": { alerts: [] },
      });

      await useAccessStore.getState().fetchAlerts(undefined, client);
      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toBe("/api/v2/governance/alerts");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Governance service down");
        }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchAlerts("zone-1", client);
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

  describe("resolveAlert", () => {
    it("resolves an alert with zone_id and updates local state", async () => {
      useAccessStore.setState({
        alerts: [
          {
            alert_id: "a-1",
            agent_id: "agent-rogue",
            zone_id: "zone-1",
            severity: "critical",
            alert_type: "access_violation",
            details: { reason: "Unauthorized" },
            resolved: false,
            created_at: "2025-01-15T10:00:00Z",
          },
        ],
      });

      const client = mockClient({
        "/api/v2/governance/alerts/a-1/resolve": {
          alert_id: "a-1",
          resolved: true,
          resolved_by: "tui-operator",
        },
      });

      await useAccessStore.getState().resolveAlert("a-1", "tui-operator", "zone-1", client);
      const state = useAccessStore.getState();

      expect(state.alerts[0]!.resolved).toBe(true);
      expect(state.alertsLoading).toBe(false);
      expect(state.error).toBeNull();

      // Verify zone_id passed as query param
      const calledUrl = (client.post as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("zone_id=zone-1");
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Not authorized"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().resolveAlert("a-1", "op", undefined, client);
      expect(useAccessStore.getState().error).toBe("Not authorized");
    });
  });

  describe("setSelectedAlertIndex", () => {
    it("sets the selected alert index", () => {
      useAccessStore.getState().setSelectedAlertIndex(2);
      expect(useAccessStore.getState().selectedAlertIndex).toBe(2);
    });
  });

  describe("fetchFraudScores", () => {
    it("fetches fraud scores with zone_id", async () => {
      const client = mockClient({
        "/api/v2/governance/fraud-scores": {
          scores: [
            {
              agent_id: "agent-alice",
              zone_id: "zone-1",
              score: 0.15,
              components: { velocity: 0.1, volume: 0.2 },
              computed_at: "2025-06-01T12:00:00Z",
            },
            {
              agent_id: "agent-bob",
              zone_id: "zone-1",
              score: 0.85,
              components: { velocity: 0.9, volume: 0.8 },
              computed_at: "2025-06-01T12:00:00Z",
            },
          ],
          count: 2,
        },
      });

      await useAccessStore.getState().fetchFraudScores("zone-1", client);
      const state = useAccessStore.getState();

      expect(state.fraudScores).toHaveLength(2);
      expect(state.fraudScores[0]!.agent_id).toBe("agent-alice");
      expect(state.fraudScores[0]!.score).toBe(0.15);
      expect(state.fraudScores[0]!.components.velocity).toBe(0.1);
      expect(state.fraudScores[1]!.score).toBe(0.85);
      expect(state.fraudScoresLoading).toBe(false);
      expect(state.selectedFraudIndex).toBe(0);
      expect(state.error).toBeNull();

      // Verify zone_id passed as query param
      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("zone_id=zone-1");
    });

    it("fetches without zone_id when undefined", async () => {
      const client = mockClient({
        "/api/v2/governance/fraud-scores": { scores: [], count: 0 },
      });

      await useAccessStore.getState().fetchFraudScores(undefined, client);
      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toBe("/api/v2/governance/fraud-scores");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Governance down"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchFraudScores("zone-1", client);
      expect(useAccessStore.getState().error).toBe("Governance down");
    });
  });

  describe("computeFraudScores", () => {
    it("computes fraud scores via POST", async () => {
      const client = mockClient({
        "/api/v2/governance/fraud-scores/compute": {
          scores: [
            {
              agent_id: "agent-x",
              zone_id: "zone-1",
              score: 0.42,
              components: { pattern: 0.5 },
              computed_at: "2025-06-02T00:00:00Z",
            },
          ],
          count: 1,
        },
      });

      await useAccessStore.getState().computeFraudScores("zone-1", client);
      const state = useAccessStore.getState();

      expect(state.fraudScores).toHaveLength(1);
      expect(state.fraudScores[0]!.score).toBe(0.42);
      expect(state.fraudScoresLoading).toBe(false);

      // Verify POST was called
      expect(client.post).toHaveBeenCalled();
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Compute failed"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().computeFraudScores("zone-1", client);
      expect(useAccessStore.getState().error).toBe("Compute failed");
    });
  });

  describe("setSelectedFraudIndex", () => {
    it("sets the selected fraud index", () => {
      useAccessStore.getState().setSelectedFraudIndex(3);
      expect(useAccessStore.getState().selectedFraudIndex).toBe(3);
    });
  });

  describe("fetchDelegations", () => {
    it("fetches and stores delegations", async () => {
      const client = mockClient({
        "/api/v2/agents/delegate": {
          delegations: [
            {
              delegation_id: "del-1",
              agent_id: "agent-child",
              parent_agent_id: "agent-parent",
              delegation_mode: "supervised",
              status: "active",
              scope_prefix: "files/reports/",
              lease_expires_at: "2025-12-31T23:59:59Z",
              zone_id: "zone-1",
              intent: "Read reports",
              depth: 1,
              can_sub_delegate: false,
              created_at: "2025-01-01T00:00:00Z",
            },
            {
              delegation_id: "del-2",
              agent_id: "agent-sub",
              parent_agent_id: "agent-child",
              delegation_mode: "autonomous",
              status: "expired",
              scope_prefix: null,
              lease_expires_at: null,
              zone_id: null,
              intent: "General access",
              depth: 2,
              can_sub_delegate: true,
              created_at: "2025-02-01T00:00:00Z",
            },
          ],
          count: 2,
        },
      });

      await useAccessStore.getState().fetchDelegations(client);
      const state = useAccessStore.getState();

      expect(state.delegations).toHaveLength(2);
      expect(state.delegations[0]!.delegation_id).toBe("del-1");
      expect(state.delegations[0]!.agent_id).toBe("agent-child");
      expect(state.delegations[0]!.parent_agent_id).toBe("agent-parent");
      expect(state.delegations[0]!.scope_prefix).toBe("files/reports/");
      expect(state.delegations[0]!.delegation_mode).toBe("supervised");
      expect(state.delegations[0]!.can_sub_delegate).toBe(false);
      expect(state.delegations[0]!.depth).toBe(1);
      expect(state.delegations[1]!.scope_prefix).toBeNull();
      expect(state.delegations[1]!.can_sub_delegate).toBe(true);
      expect(state.delegationsLoading).toBe(false);
      expect(state.selectedDelegationIndex).toBe(0);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Delegation service down"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchDelegations(client);
      expect(useAccessStore.getState().error).toBe("Delegation service down");
    });
  });

  describe("setSelectedDelegationIndex", () => {
    it("sets the selected delegation index", () => {
      useAccessStore.getState().setSelectedDelegationIndex(2);
      expect(useAccessStore.getState().selectedDelegationIndex).toBe(2);
    });
  });

  describe("createDelegation", () => {
    it("creates a delegation and refreshes list", async () => {
      const client = mockClient({
        "/api/v2/agents/delegate": {
          delegation_id: "del-new",
          worker_agent_id: "worker-1",
          api_key: "nx_live_worker1_key",
          mount_table: ["files/reports/"],
          expires_at: "2025-12-31T23:59:59Z",
          delegation_mode: "clean",
          delegations: [{
            delegation_id: "del-new",
            agent_id: "worker-1",
            parent_agent_id: "parent-1",
            delegation_mode: "clean",
            status: "active",
            scope_prefix: "files/reports/",
            lease_expires_at: "2025-12-31T23:59:59Z",
            zone_id: "zone-1",
            intent: "Read reports",
            depth: 1,
            can_sub_delegate: false,
            created_at: "2025-01-01T00:00:00Z",
          }],
          count: 1,
        },
      });

      await useAccessStore.getState().createDelegation(
        {
          worker_id: "worker-1",
          worker_name: "Report Reader",
          namespace_mode: "clean",
          scope_prefix: "files/reports/",
          intent: "Read reports",
          can_sub_delegate: false,
        },
        client,
      );
      const state = useAccessStore.getState();

      expect(state.lastDelegationCreate).not.toBeNull();
      expect(state.lastDelegationCreate!.delegation_id).toBe("del-new");
      expect(state.lastDelegationCreate!.api_key).toBe("nx_live_worker1_key");
      expect(state.lastDelegationCreate!.mount_table).toEqual(["files/reports/"]);
      expect(state.delegationsLoading).toBe(false);
    });

    it("preserves create result when list refresh fails", async () => {
      let callCount = 0;
      const client = {
        post: mock(async () => ({
          delegation_id: "del-ok",
          worker_agent_id: "worker-1",
          api_key: "key123",
          mount_table: [],
          expires_at: null,
          delegation_mode: "clean",
        })),
        get: mock(async () => {
          callCount++;
          throw new Error("List refresh failed");
        }),
      } as unknown as FetchClient;

      await useAccessStore.getState().createDelegation(
        { worker_id: "w", worker_name: "n", namespace_mode: "clean", intent: "i", can_sub_delegate: false },
        client,
      );
      const state = useAccessStore.getState();

      // POST succeeded — result must be visible
      expect(state.lastDelegationCreate).not.toBeNull();
      expect(state.lastDelegationCreate!.delegation_id).toBe("del-ok");
      // Error must NOT be set (GET failure is non-critical)
      expect(state.error).toBeNull();
      expect(state.delegationsLoading).toBe(false);
      // GET was attempted
      expect(callCount).toBe(1);
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Insufficient trust"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().createDelegation(
        { worker_id: "w", worker_name: "n", namespace_mode: "clean", intent: "i", can_sub_delegate: false },
        client,
      );
      expect(useAccessStore.getState().error).toBe("Insufficient trust");
    });
  });

  describe("revokeDelegation", () => {
    it("revokes a delegation and updates status", async () => {
      useAccessStore.setState({
        delegations: [{
          delegation_id: "del-1",
          agent_id: "worker-1",
          parent_agent_id: "parent-1",
          delegation_mode: "clean",
          status: "active",
          scope_prefix: "files/",
          lease_expires_at: null,
          zone_id: "zone-1",
          intent: "test",
          depth: 1,
          can_sub_delegate: false,
          created_at: "2025-01-01T00:00:00Z",
        }],
      });

      const client = mockClient({
        "/api/v2/agents/delegate/del-1": {
          status: "revoked",
          delegation_id: "del-1",
        },
      });

      await useAccessStore.getState().revokeDelegation("del-1", client);
      const state = useAccessStore.getState();

      expect(state.delegations[0]!.status).toBe("revoked");
      expect(state.delegationsLoading).toBe(false);
    });

    it("sets error on failure", async () => {
      const client = {
        delete: mock(async () => { throw new Error("Not authorized"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().revokeDelegation("del-1", client);
      expect(useAccessStore.getState().error).toBe("Not authorized");
    });
  });

  describe("completeDelegation", () => {
    it("completes a delegation with outcome and quality score", async () => {
      useAccessStore.setState({
        delegations: [{
          delegation_id: "del-1",
          agent_id: "worker-1",
          parent_agent_id: "parent-1",
          delegation_mode: "clean",
          status: "active",
          scope_prefix: null,
          lease_expires_at: null,
          zone_id: null,
          intent: "task",
          depth: 1,
          can_sub_delegate: false,
          created_at: "2025-01-01T00:00:00Z",
        }],
      });

      const client = mockClient({
        "/api/v2/agents/delegate/del-1/complete": {
          status: "completed",
          delegation_id: "del-1",
          outcome: "completed",
        },
      });

      await useAccessStore.getState().completeDelegation("del-1", "completed", 0.9, client);
      const state = useAccessStore.getState();

      expect(state.delegations[0]!.status).toBe("completed");
      expect(state.delegationsLoading).toBe(false);
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Already completed"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().completeDelegation("del-1", "completed", null, client);
      expect(useAccessStore.getState().error).toBe("Already completed");
    });
  });

  describe("fetchDelegationChain", () => {
    it("fetches and stores delegation chain", async () => {
      const client = mockClient({
        "/api/v2/agents/delegate/del-1/chain": {
          chain: [
            {
              delegation_id: "del-root",
              agent_id: "agent-root",
              parent_agent_id: "system",
              delegation_mode: "clean",
              status: "active",
              depth: 0,
              intent: "Root delegation",
              created_at: "2025-01-01T00:00:00Z",
            },
            {
              delegation_id: "del-1",
              agent_id: "worker-1",
              parent_agent_id: "agent-root",
              delegation_mode: "clean",
              status: "active",
              depth: 1,
              intent: "Sub-task",
              created_at: "2025-01-02T00:00:00Z",
            },
          ],
          total_depth: 1,
        },
      });

      await useAccessStore.getState().fetchDelegationChain("del-1", client);
      const state = useAccessStore.getState();

      expect(state.delegationChain).not.toBeNull();
      expect(state.delegationChain!.chain).toHaveLength(2);
      expect(state.delegationChain!.total_depth).toBe(1);
      expect(state.delegationChain!.chain[0]!.delegation_id).toBe("del-root");
      expect(state.delegationChain!.chain[1]!.delegation_id).toBe("del-1");
      expect(state.delegationChainLoading).toBe(false);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Chain not found"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchDelegationChain("del-x", client);
      expect(useAccessStore.getState().error).toBe("Chain not found");
    });
  });

  describe("checkGovernanceEdge", () => {
    it("checks governance constraint between agents with zone_id", async () => {
      const client = mockClient({
        "/api/v2/governance/check": {
          allowed: true,
          constraint_type: null,
          reason: "No constraints found",
          edge_id: "edge-123",
        },
      });

      await useAccessStore.getState().checkGovernanceEdge("agent-a", "agent-b", "zone-1", client);
      const state = useAccessStore.getState();

      expect(state.governanceCheck).not.toBeNull();
      expect(state.governanceCheck!.allowed).toBe(true);
      expect(state.governanceCheck!.reason).toBe("No constraints found");
      expect(state.governanceCheck!.edge_id).toBe("edge-123");
      expect(state.governanceCheckLoading).toBe(false);

      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("agent-a");
      expect(calledUrl).toContain("agent-b");
      expect(calledUrl).toContain("zone_id=zone-1");
    });

    it("returns blocked result with constraint details", async () => {
      const client = mockClient({
        "/api/v2/governance/check": {
          allowed: false,
          constraint_type: "blocklist",
          reason: "Agent is blocklisted",
          edge_id: "edge-456",
        },
      });

      await useAccessStore.getState().checkGovernanceEdge("agent-a", "agent-bad", undefined, client);
      const state = useAccessStore.getState();

      expect(state.governanceCheck!.allowed).toBe(false);
      expect(state.governanceCheck!.constraint_type).toBe("blocklist");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Governance unavailable"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().checkGovernanceEdge("a", "b", "z", client);
      expect(useAccessStore.getState().error).toBe("Governance unavailable");
    });
  });

  describe("checkPermission with trace", () => {
    it("stores server-side evaluation trace", async () => {
      const client = mockClient({
        "/api/v2/access-manifests/m-1/evaluate": {
          tool_name: "file:read:reports",
          permission: "allow",
          agent_id: "agent-alice",
          manifest_id: "m-1",
          trace: {
            matched_index: 0,
            default_applied: false,
            entries: [
              {
                index: 0,
                tool_pattern: "file:read:*",
                permission: "allow",
                matched: true,
                max_calls_per_minute: 100,
              },
              {
                index: 1,
                tool_pattern: "file:write:*",
                permission: "deny",
                matched: false,
                max_calls_per_minute: null,
              },
            ],
          },
        },
      });

      await useAccessStore.getState().checkPermission("m-1", "file:read:reports", client);
      const state = useAccessStore.getState();

      expect(state.lastPermissionCheck).not.toBeNull();
      expect(state.lastPermissionCheck!.trace).not.toBeNull();
      expect(state.lastPermissionCheck!.trace!.matched_index).toBe(0);
      expect(state.lastPermissionCheck!.trace!.default_applied).toBe(false);
      expect(state.lastPermissionCheck!.trace!.entries).toHaveLength(2);
      expect(state.lastPermissionCheck!.trace!.entries[0]!.matched).toBe(true);
      expect(state.lastPermissionCheck!.trace!.entries[0]!.tool_pattern).toBe("file:read:*");
      expect(state.lastPermissionCheck!.trace!.entries[1]!.matched).toBe(false);
    });

    it("handles response without trace (backward compat)", async () => {
      const client = mockClient({
        "/api/v2/access-manifests/m-2/evaluate": {
          tool_name: "tool-x",
          permission: "deny",
          agent_id: "agent-bob",
          manifest_id: "m-2",
        },
      });

      await useAccessStore.getState().checkPermission("m-2", "tool-x", client);
      const state = useAccessStore.getState();

      expect(state.lastPermissionCheck!.trace).toBeNull();
      expect(state.lastPermissionCheck!.permission).toBe("deny");
    });

    it("stores default-deny trace when no entry matches", async () => {
      const client = mockClient({
        "/api/v2/access-manifests/m-3/evaluate": {
          tool_name: "unknown-tool",
          permission: "deny",
          agent_id: "agent-x",
          manifest_id: "m-3",
          trace: {
            matched_index: -1,
            default_applied: true,
            entries: [
              {
                index: 0,
                tool_pattern: "file:*",
                permission: "allow",
                matched: false,
                max_calls_per_minute: null,
              },
            ],
          },
        },
      });

      await useAccessStore.getState().checkPermission("m-3", "unknown-tool", client);
      const state = useAccessStore.getState();

      expect(state.lastPermissionCheck!.trace!.default_applied).toBe(true);
      expect(state.lastPermissionCheck!.trace!.matched_index).toBe(-1);
      expect(state.lastPermissionCheck!.trace!.entries[0]!.matched).toBe(false);
    });
  });

  describe("fetchNamespaceDetail", () => {
    it("fetches and stores namespace detail", async () => {
      const client = mockClient({
        "/api/v2/agents/delegate/del-1/namespace": {
          delegation_id: "del-1",
          agent_id: "worker-1",
          delegation_mode: "copy",
          scope_prefix: "files/reports/",
          removed_grants: ["/admin"],
          added_grants: [],
          readonly_paths: ["/config"],
          mount_table: ["/workspace/reports", "/workspace/shared"],
          zone_id: "zone-1",
        },
      });

      await useAccessStore.getState().fetchNamespaceDetail("del-1", client);
      const state = useAccessStore.getState();

      expect(state.namespaceDetail).not.toBeNull();
      expect(state.namespaceDetail!.delegation_id).toBe("del-1");
      expect(state.namespaceDetail!.delegation_mode).toBe("copy");
      expect(state.namespaceDetail!.scope_prefix).toBe("files/reports/");
      expect(state.namespaceDetail!.removed_grants).toEqual(["/admin"]);
      expect(state.namespaceDetail!.readonly_paths).toEqual(["/config"]);
      expect(state.namespaceDetail!.mount_table).toEqual(["/workspace/reports", "/workspace/shared"]);
      expect(state.namespaceDetailLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("handles clean mode with added grants", async () => {
      const client = mockClient({
        "/api/v2/agents/delegate/del-2/namespace": {
          delegation_id: "del-2",
          agent_id: "worker-2",
          delegation_mode: "clean",
          scope_prefix: null,
          removed_grants: [],
          added_grants: ["/workspace/sandbox/a.txt", "/workspace/sandbox/b.txt"],
          readonly_paths: [],
          mount_table: ["/workspace/sandbox"],
          zone_id: null,
        },
      });

      await useAccessStore.getState().fetchNamespaceDetail("del-2", client);
      const state = useAccessStore.getState();

      expect(state.namespaceDetail!.delegation_mode).toBe("clean");
      expect(state.namespaceDetail!.added_grants).toHaveLength(2);
      expect(state.namespaceDetail!.scope_prefix).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Namespace not found");
        }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchNamespaceDetail("del-404", client);
      const state = useAccessStore.getState();

      expect(state.namespaceDetailLoading).toBe(false);
      expect(state.error).toBe("Namespace not found");
      expect(state.namespaceDetail).toBeNull();
    });
  });

  describe("updateNamespaceConfig", () => {
    it("sends PATCH and stores updated namespace", async () => {
      const client = mockClient({
        "/api/v2/agents/delegate/del-1/namespace": {
          delegation_id: "del-1",
          agent_id: "worker-1",
          delegation_mode: "copy",
          scope_prefix: "files/new-prefix/",
          removed_grants: ["/admin", "/secrets"],
          added_grants: [],
          readonly_paths: ["/config"],
          mount_table: ["/workspace/reports"],
          zone_id: "zone-1",
        },
      });

      await useAccessStore.getState().updateNamespaceConfig(
        "del-1",
        {
          scope_prefix: "files/new-prefix/",
          remove_grants: ["/admin", "/secrets"],
        },
        client,
      );
      const state = useAccessStore.getState();

      expect(state.namespaceDetail).not.toBeNull();
      expect(state.namespaceDetail!.scope_prefix).toBe("files/new-prefix/");
      expect(state.namespaceDetail!.removed_grants).toEqual(["/admin", "/secrets"]);
      expect(state.namespaceDetailLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("calls patch method on client", async () => {
      const patchMock = mock(async () => ({
        delegation_id: "del-1",
        agent_id: "worker-1",
        delegation_mode: "copy",
        scope_prefix: null,
        removed_grants: [],
        added_grants: ["/new/path"],
        readonly_paths: [],
        mount_table: [],
        zone_id: null,
      }));
      const client = {
        patch: patchMock,
      } as unknown as FetchClient;

      await useAccessStore.getState().updateNamespaceConfig(
        "del-1",
        { add_grants: ["/new/path"] },
        client,
      );
      expect(patchMock).toHaveBeenCalledWith(
        "/api/v2/agents/delegate/del-1/namespace",
        { add_grants: ["/new/path"] },
      );
    });

    it("sets error on failure", async () => {
      const client = {
        patch: mock(async () => {
          throw new Error("Forbidden");
        }),
      } as unknown as FetchClient;

      await useAccessStore.getState().updateNamespaceConfig(
        "del-1",
        { scope_prefix: "x" },
        client,
      );
      const state = useAccessStore.getState();

      expect(state.namespaceDetailLoading).toBe(false);
      expect(state.error).toBe("Forbidden");
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
