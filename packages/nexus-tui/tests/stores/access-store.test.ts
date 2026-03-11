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
    scores: [],
    scoresLoading: false,
    leaderboard: [],
    leaderboardLoading: false,
    credentials: [],
    credentialsLoading: false,
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
    it("fetches and stores access manifests", async () => {
      const client = mockClient({
        "/api/v2/access/manifests": {
          manifests: [
            {
              manifest_id: "m-1",
              subject: "agent-alice",
              relation: "can_read",
              object: "file:/data/reports",
              zone_id: "zone-1",
              granted_at: "2025-01-01T00:00:00Z",
              expires_at: "2025-12-31T23:59:59Z",
              granted_by: "admin",
            },
            {
              manifest_id: "m-2",
              subject: "agent-bob",
              relation: "can_write",
              object: "file:/data/logs",
              zone_id: null,
              granted_at: "2025-02-01T00:00:00Z",
              expires_at: null,
              granted_by: "agent-alice",
            },
          ],
        },
      });

      await useAccessStore.getState().fetchManifests(client);
      const state = useAccessStore.getState();

      expect(state.manifests).toHaveLength(2);
      expect(state.manifests[0]!.manifest_id).toBe("m-1");
      expect(state.manifests[0]!.subject).toBe("agent-alice");
      expect(state.manifests[0]!.relation).toBe("can_read");
      expect(state.manifests[0]!.object).toBe("file:/data/reports");
      expect(state.manifests[0]!.zone_id).toBe("zone-1");
      expect(state.manifests[1]!.zone_id).toBeNull();
      expect(state.manifestsLoading).toBe(false);
      expect(state.selectedManifestIndex).toBe(0);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Access service unavailable"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchManifests(client);
      const state = useAccessStore.getState();
      expect(state.manifestsLoading).toBe(false);
      expect(state.error).toBe("Access service unavailable");
    });

    it("resets selectedManifestIndex on refetch", async () => {
      useAccessStore.setState({ selectedManifestIndex: 5 });
      const client = mockClient({
        "/api/v2/access/manifests": { manifests: [] },
      });

      await useAccessStore.getState().fetchManifests(client);
      expect(useAccessStore.getState().selectedManifestIndex).toBe(0);
    });
  });

  describe("checkPermission", () => {
    it("checks permission and stores result", async () => {
      const client = mockClient({
        "/api/v2/access/check": {
          allowed: true,
          reason: "Direct grant via manifest m-1",
        },
      });

      await useAccessStore.getState().checkPermission(
        "agent-alice",
        "can_read",
        "file:/data/reports",
        client,
      );
      const state = useAccessStore.getState();

      expect(state.lastPermissionCheck).not.toBeNull();
      expect(state.lastPermissionCheck!.allowed).toBe(true);
      expect(state.lastPermissionCheck!.reason).toBe("Direct grant via manifest m-1");
      expect(state.lastPermissionCheck!.checked_at).toBeTruthy();
      expect(state.permissionCheckLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("stores denied permission check", async () => {
      const client = mockClient({
        "/api/v2/access/check": {
          allowed: false,
          reason: "No matching manifest found",
        },
      });

      await useAccessStore.getState().checkPermission(
        "agent-bob",
        "can_delete",
        "file:/data/critical",
        client,
      );
      const state = useAccessStore.getState();

      expect(state.lastPermissionCheck!.allowed).toBe(false);
      expect(state.lastPermissionCheck!.reason).toBe("No matching manifest found");
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Permission check failed"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().checkPermission(
        "agent-alice",
        "can_read",
        "file:/data",
        client,
      );
      const state = useAccessStore.getState();
      expect(state.permissionCheckLoading).toBe(false);
      expect(state.error).toBe("Permission check failed");
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
        get: mock(async () => { throw new Error("Governance service down"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchAlerts(client);
      const state = useAccessStore.getState();
      expect(state.alertsLoading).toBe(false);
      expect(state.error).toBe("Governance service down");
    });
  });

  describe("fetchScores", () => {
    it("fetches and stores reputation scores", async () => {
      const client = mockClient({
        "/api/v2/governance/reputation/scores": {
          scores: [
            {
              agent_id: "agent-alice",
              score: 92,
              trust_level: "high",
              last_updated: "2025-01-15T12:00:00Z",
            },
            {
              agent_id: "agent-bob",
              score: 45,
              trust_level: "medium",
              last_updated: "2025-01-14T08:00:00Z",
            },
          ],
        },
      });

      await useAccessStore.getState().fetchScores(client);
      const state = useAccessStore.getState();

      expect(state.scores).toHaveLength(2);
      expect(state.scores[0]!.agent_id).toBe("agent-alice");
      expect(state.scores[0]!.score).toBe(92);
      expect(state.scores[0]!.trust_level).toBe("high");
      expect(state.scores[1]!.score).toBe(45);
      expect(state.scoresLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Reputation service unavailable"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchScores(client);
      const state = useAccessStore.getState();
      expect(state.scoresLoading).toBe(false);
      expect(state.error).toBe("Reputation service unavailable");
    });
  });

  describe("fetchLeaderboard", () => {
    it("fetches and stores leaderboard entries", async () => {
      const client = mockClient({
        "/api/v2/governance/leaderboard": {
          entries: [
            { rank: 1, agent_id: "agent-alice", score: 92, trust_level: "high" },
            { rank: 2, agent_id: "agent-charlie", score: 88, trust_level: "high" },
            { rank: 3, agent_id: "agent-bob", score: 45, trust_level: "medium" },
          ],
        },
      });

      await useAccessStore.getState().fetchLeaderboard(client);
      const state = useAccessStore.getState();

      expect(state.leaderboard).toHaveLength(3);
      expect(state.leaderboard[0]!.rank).toBe(1);
      expect(state.leaderboard[0]!.agent_id).toBe("agent-alice");
      expect(state.leaderboard[2]!.rank).toBe(3);
      expect(state.leaderboard[2]!.trust_level).toBe("medium");
      expect(state.leaderboardLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Leaderboard unavailable"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchLeaderboard(client);
      const state = useAccessStore.getState();
      expect(state.leaderboardLoading).toBe(false);
      expect(state.error).toBe("Leaderboard unavailable");
    });
  });

  describe("fetchCredentials", () => {
    it("fetches and stores credentials", async () => {
      const client = mockClient({
        "/api/v2/credentials": {
          credentials: [
            {
              credential_id: "cred-1",
              type: "api_key",
              issuer: "nexus-ca",
              subject: "agent-alice",
              issued_at: "2025-01-01T00:00:00Z",
              expires_at: "2025-12-31T23:59:59Z",
              status: "active",
            },
            {
              credential_id: "cred-2",
              type: "x509_cert",
              issuer: "nexus-ca",
              subject: "agent-bob",
              issued_at: "2024-06-01T00:00:00Z",
              expires_at: "2024-12-31T23:59:59Z",
              status: "expired",
            },
            {
              credential_id: "cred-3",
              type: "jwt",
              issuer: "auth-service",
              subject: "agent-charlie",
              issued_at: "2025-01-10T00:00:00Z",
              expires_at: null,
              status: "revoked",
            },
          ],
        },
      });

      await useAccessStore.getState().fetchCredentials(client);
      const state = useAccessStore.getState();

      expect(state.credentials).toHaveLength(3);
      expect(state.credentials[0]!.credential_id).toBe("cred-1");
      expect(state.credentials[0]!.type).toBe("api_key");
      expect(state.credentials[0]!.status).toBe("active");
      expect(state.credentials[1]!.status).toBe("expired");
      expect(state.credentials[2]!.status).toBe("revoked");
      expect(state.credentials[2]!.expires_at).toBeNull();
      expect(state.credentialsLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Credentials service down"); }),
      } as unknown as FetchClient;

      await useAccessStore.getState().fetchCredentials(client);
      const state = useAccessStore.getState();
      expect(state.credentialsLoading).toBe(false);
      expect(state.error).toBe("Credentials service down");
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
        "/api/v2/access/manifests": { manifests: [] },
      });

      await useAccessStore.getState().fetchManifests(client);
      expect(useAccessStore.getState().error).toBeNull();
    });
  });
});
