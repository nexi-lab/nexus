import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useAgentsStore } from "../../src/stores/agents-store.js";
import type { FetchClient } from "@nexus/api-client";

function mockClient(responses: Record<string, unknown>): FetchClient {
  return {
    get: mock(async (path: string) => {
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
  useAgentsStore.setState({
    knownAgents: [],
    selectedAgentId: null,
    selectedAgentIndex: 0,
    activeTab: "status",
    agentStatus: null,
    agentSpec: null,
    agentIdentity: null,
    statusLoading: false,
    delegations: [],
    delegationsLoading: false,
    selectedDelegationIndex: 0,
    inboxMessages: [],
    inboxCount: 0,
    inboxLoading: false,
    error: null,
  });
}

describe("AgentsStore", () => {
  beforeEach(() => {
    resetStore();
  });

  describe("setActiveTab", () => {
    it("switches between tabs", () => {
      useAgentsStore.getState().setActiveTab("delegations");
      expect(useAgentsStore.getState().activeTab).toBe("delegations");

      useAgentsStore.getState().setActiveTab("inbox");
      expect(useAgentsStore.getState().activeTab).toBe("inbox");

      useAgentsStore.getState().setActiveTab("status");
      expect(useAgentsStore.getState().activeTab).toBe("status");
    });
  });

  describe("addKnownAgent", () => {
    it("adds a new agent to the list", () => {
      useAgentsStore.getState().addKnownAgent("agent-1");
      expect(useAgentsStore.getState().knownAgents).toEqual(["agent-1"]);
    });

    it("does not add duplicate agents", () => {
      useAgentsStore.getState().addKnownAgent("agent-1");
      useAgentsStore.getState().addKnownAgent("agent-1");
      expect(useAgentsStore.getState().knownAgents).toEqual(["agent-1"]);
    });

    it("adds multiple distinct agents", () => {
      useAgentsStore.getState().addKnownAgent("agent-1");
      useAgentsStore.getState().addKnownAgent("agent-2");
      expect(useAgentsStore.getState().knownAgents).toEqual(["agent-1", "agent-2"]);
    });
  });

  describe("setSelectedAgentId", () => {
    it("sets selected agent and clears error", () => {
      useAgentsStore.setState({ error: "old error" });
      useAgentsStore.getState().setSelectedAgentId("agent-42");
      const state = useAgentsStore.getState();
      expect(state.selectedAgentId).toBe("agent-42");
      expect(state.error).toBeNull();
    });
  });

  describe("fetchAgentStatus", () => {
    it("fetches and stores agent status", async () => {
      const client = mockClient({
        "/api/v2/agents/agent-1/status": {
          phase: "active",
          observed_generation: 3,
          conditions: [
            { type: "Ready", status: "True", reason: "Healthy", message: "OK", last_transition: "2025-01-01T00:00:00Z" },
          ],
          resource_usage: { tokens_used: 500, storage_used_mb: 10, context_usage_pct: 45 },
          last_heartbeat: "2025-01-01T12:00:00Z",
          last_activity: "2025-01-01T11:55:00Z",
          inbox_depth: 3,
          context_usage_pct: 45,
        },
      });

      await useAgentsStore.getState().fetchAgentStatus("agent-1", client);
      const state = useAgentsStore.getState();

      expect(state.agentStatus).not.toBeNull();
      expect(state.agentStatus!.agent_id).toBe("agent-1");
      expect(state.agentStatus!.phase).toBe("active");
      expect(state.agentStatus!.observed_generation).toBe(3);
      expect(state.agentStatus!.conditions).toHaveLength(1);
      expect(state.agentStatus!.resource_usage.tokens_used).toBe(500);
      expect(state.agentStatus!.inbox_depth).toBe(3);
      expect(state.statusLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Agent not found"); }),
      } as unknown as FetchClient;

      await useAgentsStore.getState().fetchAgentStatus("missing-agent", client);
      const state = useAgentsStore.getState();
      expect(state.agentStatus).toBeNull();
      expect(state.statusLoading).toBe(false);
      expect(state.error).toBe("Agent not found");
    });
  });

  describe("fetchAgentSpec", () => {
    it("fetches and stores agent spec", async () => {
      const client = mockClient({
        "/api/v2/agents/agent-1/spec": {
          agent_type: "worker",
          capabilities: ["read", "write", "execute"],
          qos_class: "guaranteed",
          zone_affinity: "us-east",
          spec_generation: 2,
        },
      });

      await useAgentsStore.getState().fetchAgentSpec("agent-1", client);
      const state = useAgentsStore.getState();

      expect(state.agentSpec).not.toBeNull();
      expect(state.agentSpec!.agent_type).toBe("worker");
      expect(state.agentSpec!.capabilities).toEqual(["read", "write", "execute"]);
      expect(state.agentSpec!.qos_class).toBe("guaranteed");
      expect(state.agentSpec!.zone_affinity).toBe("us-east");
      expect(state.agentSpec!.spec_generation).toBe(2);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Spec unavailable"); }),
      } as unknown as FetchClient;

      await useAgentsStore.getState().fetchAgentSpec("agent-1", client);
      expect(useAgentsStore.getState().error).toBe("Spec unavailable");
    });
  });

  describe("fetchAgentIdentity", () => {
    it("fetches and stores agent identity", async () => {
      const client = mockClient({
        "/api/v2/agents/agent-1/identity": {
          agent_id: "agent-1",
          key_id: "key-abc123",
          did: "did:nexus:agent-1",
          algorithm: "Ed25519",
          public_key_hex: "abcdef0123456789abcdef0123456789",
          created_at: "2025-01-01T00:00:00Z",
          expires_at: null,
        },
      });

      await useAgentsStore.getState().fetchAgentIdentity("agent-1", client);
      const state = useAgentsStore.getState();

      expect(state.agentIdentity).not.toBeNull();
      expect(state.agentIdentity!.agent_id).toBe("agent-1");
      expect(state.agentIdentity!.did).toBe("did:nexus:agent-1");
      expect(state.agentIdentity!.algorithm).toBe("Ed25519");
      expect(state.agentIdentity!.key_id).toBe("key-abc123");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Identity not found"); }),
      } as unknown as FetchClient;

      await useAgentsStore.getState().fetchAgentIdentity("agent-1", client);
      expect(useAgentsStore.getState().error).toBe("Identity not found");
    });
  });

  describe("fetchDelegations", () => {
    it("fetches and stores delegations list", async () => {
      const client = mockClient({
        "/api/v2/agents/delegate": {
          delegations: [
            {
              delegation_id: "del-1",
              agent_id: "agent-1",
              parent_agent_id: "agent-0",
              delegation_mode: "copy",
              status: "active",
              scope_prefix: "/data",
              lease_expires_at: "2025-12-31T23:59:59Z",
              zone_id: "zone-1",
              intent: "process data",
              depth: 1,
              can_sub_delegate: true,
              created_at: "2025-01-01T00:00:00Z",
            },
            {
              delegation_id: "del-2",
              agent_id: "agent-2",
              parent_agent_id: "agent-0",
              delegation_mode: "clean",
              status: "completed",
              scope_prefix: null,
              lease_expires_at: null,
              zone_id: null,
              intent: "analyze logs",
              depth: 1,
              can_sub_delegate: false,
              created_at: "2025-01-02T00:00:00Z",
            },
          ],
          total: 2,
        },
      });

      await useAgentsStore.getState().fetchDelegations(client);
      const state = useAgentsStore.getState();

      expect(state.delegations).toHaveLength(2);
      expect(state.delegations[0]!.delegation_id).toBe("del-1");
      expect(state.delegations[0]!.delegation_mode).toBe("copy");
      expect(state.delegations[1]!.status).toBe("completed");
      expect(state.delegationsLoading).toBe(false);
      expect(state.selectedDelegationIndex).toBe(0);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Delegation service down"); }),
      } as unknown as FetchClient;

      await useAgentsStore.getState().fetchDelegations(client);
      const state = useAgentsStore.getState();
      expect(state.delegationsLoading).toBe(false);
      expect(state.error).toBe("Delegation service down");
    });
  });

  describe("fetchInbox", () => {
    it("fetches and stores inbox messages", async () => {
      const client = mockClient({
        "/api/v2/ipc/inbox/agent-1": {
          agent_id: "agent-1",
          messages: [
            { filename: "msg-001.json" },
            { filename: "msg-002.json" },
            { filename: "msg-003.json" },
          ],
          total: 3,
        },
      });

      await useAgentsStore.getState().fetchInbox("agent-1", client);
      const state = useAgentsStore.getState();

      expect(state.inboxMessages).toHaveLength(3);
      expect(state.inboxMessages[0]!.filename).toBe("msg-001.json");
      expect(state.inboxCount).toBe(3);
      expect(state.inboxLoading).toBe(false);
    });

    it("returns empty on failure (graceful degradation)", async () => {
      const client = {
        get: mock(async () => { throw new Error("Inbox unavailable"); }),
      } as unknown as FetchClient;

      await useAgentsStore.getState().fetchInbox("agent-1", client);
      const state = useAgentsStore.getState();
      expect(state.inboxLoading).toBe(false);
      // All three fetches (inbox, processed, dead_letter) catch errors
      // and return empty arrays — no error propagated
      expect(state.inboxMessages.length).toBe(0);
      expect(state.inboxCount).toBe(0);
    });
  });

  describe("revokeDelegation", () => {
    it("calls DELETE and updates local status", async () => {
      useAgentsStore.setState({
        delegations: [
          {
            delegation_id: "del-1",
            agent_id: "agent-1",
            parent_agent_id: "agent-0",
            delegation_mode: "copy",
            status: "active",
            scope_prefix: null,
            lease_expires_at: null,
            zone_id: null,
            intent: "test",
            depth: 1,
            can_sub_delegate: false,
            created_at: "2025-01-01T00:00:00Z",
          },
        ],
      });

      const client = mockClient({
        "/api/v2/agents/delegate/del-1": {
          status: "revoked",
          delegation_id: "del-1",
        },
      });

      await useAgentsStore.getState().revokeDelegation("del-1", client);
      const state = useAgentsStore.getState();

      expect(state.delegations[0]!.status).toBe("revoked");
      expect(state.error).toBeNull();
      expect((client.delete as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    });

    it("sets error on failure", async () => {
      useAgentsStore.setState({
        delegations: [
          {
            delegation_id: "del-1",
            agent_id: "agent-1",
            parent_agent_id: "agent-0",
            delegation_mode: "copy",
            status: "active",
            scope_prefix: null,
            lease_expires_at: null,
            zone_id: null,
            intent: "test",
            depth: 1,
            can_sub_delegate: false,
            created_at: "2025-01-01T00:00:00Z",
          },
        ],
      });

      const client = {
        get: mock(async () => { throw new Error("Not allowed"); }),
        delete: mock(async () => { throw new Error("Revocation denied"); }),
      } as unknown as FetchClient;

      await useAgentsStore.getState().revokeDelegation("del-1", client);
      expect(useAgentsStore.getState().error).toBe("Revocation denied");
    });
  });

  describe("setSelectedDelegationIndex", () => {
    it("sets the selected delegation index", () => {
      useAgentsStore.getState().setSelectedDelegationIndex(5);
      expect(useAgentsStore.getState().selectedDelegationIndex).toBe(5);
    });
  });

  describe("setSelectedAgentIndex", () => {
    it("sets the selected agent index", () => {
      useAgentsStore.getState().setSelectedAgentIndex(3);
      expect(useAgentsStore.getState().selectedAgentIndex).toBe(3);
    });
  });

  describe("error handling", () => {
    it("clears error when selecting a new agent", () => {
      useAgentsStore.setState({ error: "previous error" });
      useAgentsStore.getState().setSelectedAgentId("agent-new");
      expect(useAgentsStore.getState().error).toBeNull();
    });

    it("fetchAgentStatus clears previous error on success", async () => {
      useAgentsStore.setState({ error: "old error" });

      const client = mockClient({
        "/api/v2/agents/agent-1/status": {
          phase: "idle",
          observed_generation: 1,
          conditions: [],
          resource_usage: { tokens_used: 0, storage_used_mb: 0, context_usage_pct: 0 },
          last_heartbeat: null,
          last_activity: null,
          inbox_depth: 0,
          context_usage_pct: 0,
        },
      });

      await useAgentsStore.getState().fetchAgentStatus("agent-1", client);
      // Note: fetchAgentStatus sets error to null at start
      expect(useAgentsStore.getState().error).toBeNull();
    });
  });
});
