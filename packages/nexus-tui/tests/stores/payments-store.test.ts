import { describe, it, expect, beforeEach, mock } from "bun:test";
import { usePaymentsStore } from "../../src/stores/payments-store.js";
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
    postNoContent: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return;
      }
      throw new Error(`Unmocked path: ${path}`);
    }),
  } as unknown as FetchClient;
}

function resetStore(): void {
  usePaymentsStore.setState({
    balance: null,
    balanceLoading: false,
    reservations: [],
    selectedReservationIndex: 0,
    reservationsLoading: false,
    policies: [],
    policiesLoading: false,
    auditEntries: [],
    auditTotal: 0,
    auditLoading: false,
    activeTab: "balance",
    error: null,
  });
}

describe("PaymentsStore", () => {
  beforeEach(() => {
    resetStore();
  });

  // ---------------------------------------------------------------------------
  // setActiveTab
  // ---------------------------------------------------------------------------

  describe("setActiveTab", () => {
    it("switches between tabs", () => {
      usePaymentsStore.getState().setActiveTab("reservations");
      expect(usePaymentsStore.getState().activeTab).toBe("reservations");

      usePaymentsStore.getState().setActiveTab("policies");
      expect(usePaymentsStore.getState().activeTab).toBe("policies");

      usePaymentsStore.getState().setActiveTab("audit");
      expect(usePaymentsStore.getState().activeTab).toBe("audit");

      usePaymentsStore.getState().setActiveTab("balance");
      expect(usePaymentsStore.getState().activeTab).toBe("balance");
    });

    it("clears error when switching tabs", () => {
      usePaymentsStore.setState({ error: "previous error" });
      usePaymentsStore.getState().setActiveTab("reservations");
      expect(usePaymentsStore.getState().error).toBeNull();
    });
  });

  // ---------------------------------------------------------------------------
  // setSelectedReservationIndex
  // ---------------------------------------------------------------------------

  describe("setSelectedReservationIndex", () => {
    it("sets the selected reservation index", () => {
      usePaymentsStore.getState().setSelectedReservationIndex(3);
      expect(usePaymentsStore.getState().selectedReservationIndex).toBe(3);
    });
  });

  // ---------------------------------------------------------------------------
  // fetchBalance
  // ---------------------------------------------------------------------------

  describe("fetchBalance", () => {
    it("fetches and stores balance info", async () => {
      const client = mockClient({
        "/api/v2/pay/balance": {
          available: "1000.00",
          reserved: "200.00",
          total: "1200.00",
        },
      });

      await usePaymentsStore.getState().fetchBalance(client);
      const state = usePaymentsStore.getState();

      expect(state.balance).not.toBeNull();
      expect(state.balance!.available).toBe("1000.00");
      expect(state.balance!.reserved).toBe("200.00");
      expect(state.balance!.total).toBe("1200.00");
      expect(state.balanceLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Balance service unavailable");
        }),
      } as unknown as FetchClient;

      await usePaymentsStore.getState().fetchBalance(client);
      const state = usePaymentsStore.getState();
      expect(state.balance).toBeNull();
      expect(state.balanceLoading).toBe(false);
      expect(state.error).toBe("Balance service unavailable");
    });

    it("clears previous error on success", async () => {
      usePaymentsStore.setState({ error: "old error" });

      const client = mockClient({
        "/api/v2/pay/balance": {
          available: "500.00",
          reserved: "0.00",
          total: "500.00",
        },
      });

      await usePaymentsStore.getState().fetchBalance(client);
      expect(usePaymentsStore.getState().error).toBeNull();
    });
  });

  // ---------------------------------------------------------------------------
  // transfer
  // ---------------------------------------------------------------------------

  describe("transfer", () => {
    it("calls POST with to/amount/memo and refreshes balance", async () => {
      const client = mockClient({
        "/api/v2/pay/transfer": {
          id: "txfr-001",
          method: "credits",
          amount: "100.00",
          from_agent: "agent-1",
          to_agent: "agent-2",
          memo: "test payment",
          timestamp: "2025-06-01T12:00:00Z",
          tx_hash: null,
        },
        "/api/v2/pay/balance": {
          available: "900.00",
          reserved: "0.00",
          total: "900.00",
        },
      });

      await usePaymentsStore.getState().transfer("agent-2", "100.00", "test payment", client);
      const state = usePaymentsStore.getState();

      expect(state.error).toBeNull();
      expect(state.balance).not.toBeNull();
      expect(state.balance!.available).toBe("900.00");
      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => ({})),
        post: mock(async () => {
          throw new Error("Insufficient funds");
        }),
      } as unknown as FetchClient;

      await usePaymentsStore.getState().transfer("agent-2", "999999.00", "", client);
      expect(usePaymentsStore.getState().error).toBe("Insufficient funds");
    });
  });

  // ---------------------------------------------------------------------------
  // createReservation
  // ---------------------------------------------------------------------------

  describe("createReservation", () => {
    it("calls POST with amount/purpose/timeout and stores reservation locally", async () => {
      const client = mockClient({
        "/api/v2/pay/reserve": {
          id: "res-001",
          amount: "50.00",
          purpose: "Hold for job",
          expires_at: "2025-06-02T12:00:00Z",
          status: "pending",
        },
      });

      await usePaymentsStore
        .getState()
        .createReservation("50.00", "Hold for job", 300, client);
      const state = usePaymentsStore.getState();

      expect(state.error).toBeNull();
      expect(state.reservations).toHaveLength(1);
      expect(state.reservations[0]!.id).toBe("res-001");
      expect(state.reservations[0]!.status).toBe("pending");
      expect(state.reservations[0]!.purpose).toBe("Hold for job");
    });

    it("appends to existing reservations", async () => {
      usePaymentsStore.setState({
        reservations: [
          {
            id: "res-existing",
            amount: "25.00",
            purpose: "Previous",
            expires_at: "2025-06-02T10:00:00Z",
            status: "pending",
          },
        ],
      });

      const client = mockClient({
        "/api/v2/pay/reserve": {
          id: "res-002",
          amount: "75.00",
          purpose: "New hold",
          expires_at: "2025-06-02T12:00:00Z",
          status: "pending",
        },
      });

      await usePaymentsStore
        .getState()
        .createReservation("75.00", "New hold", 300, client);
      const state = usePaymentsStore.getState();

      expect(state.reservations).toHaveLength(2);
      expect(state.reservations[0]!.id).toBe("res-existing");
      expect(state.reservations[1]!.id).toBe("res-002");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => ({})),
        post: mock(async () => {
          throw new Error("Reservation limit exceeded");
        }),
      } as unknown as FetchClient;

      await usePaymentsStore
        .getState()
        .createReservation("50.00", "test", 300, client);
      expect(usePaymentsStore.getState().error).toBe(
        "Reservation limit exceeded",
      );
    });
  });

  // ---------------------------------------------------------------------------
  // commitReservation
  // ---------------------------------------------------------------------------

  describe("commitReservation", () => {
    it("calls POST commit (204) and updates local status", async () => {
      usePaymentsStore.setState({
        reservations: [
          {
            id: "res-001",
            amount: "50.00",
            purpose: "Hold for job",
            expires_at: "2025-06-02T12:00:00Z",
            status: "pending",
          },
        ],
      });

      const client = mockClient({
        "/api/v2/pay/reserve/res-001/commit": undefined,
      });

      await usePaymentsStore.getState().commitReservation("res-001", client);
      const state = usePaymentsStore.getState();

      expect(state.error).toBeNull();
      expect(state.reservations[0]!.status).toBe("committed");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => ({})),
        postNoContent: mock(async () => {
          throw new Error("Reservation not found");
        }),
      } as unknown as FetchClient;

      await usePaymentsStore.getState().commitReservation("res-999", client);
      expect(usePaymentsStore.getState().error).toBe("Reservation not found");
    });
  });

  // ---------------------------------------------------------------------------
  // releaseReservation
  // ---------------------------------------------------------------------------

  describe("releaseReservation", () => {
    it("calls POST release (204) and updates local status", async () => {
      usePaymentsStore.setState({
        reservations: [
          {
            id: "res-001",
            amount: "50.00",
            purpose: "Hold for job",
            expires_at: "2025-06-02T12:00:00Z",
            status: "pending",
          },
        ],
      });

      const client = mockClient({
        "/api/v2/pay/reserve/res-001/release": undefined,
      });

      await usePaymentsStore.getState().releaseReservation("res-001", client);
      const state = usePaymentsStore.getState();

      expect(state.error).toBeNull();
      expect(state.reservations[0]!.status).toBe("released");
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => ({})),
        postNoContent: mock(async () => {
          throw new Error("Cannot release committed reservation");
        }),
      } as unknown as FetchClient;

      await usePaymentsStore.getState().releaseReservation("res-001", client);
      expect(usePaymentsStore.getState().error).toBe(
        "Cannot release committed reservation",
      );
    });
  });

  // ---------------------------------------------------------------------------
  // fetchPolicies
  // ---------------------------------------------------------------------------

  describe("fetchPolicies", () => {
    it("fetches and stores policies list", async () => {
      const client = mockClient({
        "/api/v2/pay/policies": {
          policies: [
            {
              policy_id: "pol-001",
              name: "Daily spending limit",
              type: "spending_limit",
              limit_amount: "10000.00",
              period: "daily",
              enabled: true,
            },
            {
              policy_id: "pol-002",
              name: "Transfer whitelist",
              type: "whitelist",
              limit_amount: null,
              period: null,
              enabled: false,
            },
          ],
        },
      });

      await usePaymentsStore.getState().fetchPolicies(client);
      const state = usePaymentsStore.getState();

      expect(state.policies).toHaveLength(2);
      expect(state.policies[0]!.policy_id).toBe("pol-001");
      expect(state.policies[0]!.name).toBe("Daily spending limit");
      expect(state.policies[0]!.enabled).toBe(true);
      expect(state.policies[1]!.limit_amount).toBeNull();
      expect(state.policies[1]!.enabled).toBe(false);
      expect(state.policiesLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Policies not available");
        }),
      } as unknown as FetchClient;

      await usePaymentsStore.getState().fetchPolicies(client);
      const state = usePaymentsStore.getState();
      expect(state.policiesLoading).toBe(false);
      expect(state.error).toBe("Policies not available");
    });
  });

  // ---------------------------------------------------------------------------
  // fetchAudit
  // ---------------------------------------------------------------------------

  describe("fetchAudit", () => {
    it("fetches and stores audit entries", async () => {
      const client = mockClient({
        "/api/v2/audit/transactions": {
          transactions: [
            {
              entry_id: "aud-001",
              type: "transfer",
              amount: "100.00",
              from_account: "acct-001",
              to_account: "acct-002",
              status: "completed",
              created_at: "2025-06-01T12:00:00Z",
              description: "Payment for service",
            },
            {
              entry_id: "aud-002",
              type: "reservation",
              amount: "50.00",
              from_account: "acct-001",
              to_account: null,
              status: "committed",
              created_at: "2025-06-01T11:00:00Z",
              description: null,
            },
          ],
          total: 42,
        },
      });

      await usePaymentsStore.getState().fetchAudit(client);
      const state = usePaymentsStore.getState();

      expect(state.auditEntries).toHaveLength(2);
      expect(state.auditEntries[0]!.entry_id).toBe("aud-001");
      expect(state.auditEntries[0]!.type).toBe("transfer");
      expect(state.auditEntries[0]!.from_account).toBe("acct-001");
      expect(state.auditEntries[1]!.to_account).toBeNull();
      expect(state.auditTotal).toBe(42);
      expect(state.auditLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Audit log unreachable");
        }),
      } as unknown as FetchClient;

      await usePaymentsStore.getState().fetchAudit(client);
      const state = usePaymentsStore.getState();
      expect(state.auditLoading).toBe(false);
      expect(state.error).toBe("Audit log unreachable");
    });
  });

  // ---------------------------------------------------------------------------
  // error handling
  // ---------------------------------------------------------------------------

  describe("error handling", () => {
    it("fetchBalance clears previous error on start", async () => {
      usePaymentsStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/pay/balance": {
          available: "100.00",
          reserved: "0.00",
          total: "100.00",
        },
      });

      await usePaymentsStore.getState().fetchBalance(client);
      expect(usePaymentsStore.getState().error).toBeNull();
    });

    it("transfer clears previous error on start", async () => {
      usePaymentsStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/pay/transfer": {
          id: "txfr-001",
          method: "credits",
          amount: "10.00",
          from_agent: "agent-1",
          to_agent: "agent-2",
          memo: "",
          timestamp: "2025-06-01T12:00:00Z",
          tx_hash: null,
        },
        "/api/v2/pay/balance": {
          available: "990.00",
          reserved: "0.00",
          total: "990.00",
        },
      });

      await usePaymentsStore.getState().transfer("agent-2", "10.00", "", client);
      expect(usePaymentsStore.getState().error).toBeNull();
    });
  });
});
