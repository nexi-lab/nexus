import { describe, it, expect, beforeEach, mock } from "bun:test";
import { usePaymentsStore } from "../../src/stores/payments-store.js";
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
    postNoContent: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return;
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
  usePaymentsStore.setState({
    balance: null,
    balanceLoading: false,
    reservations: [],
    selectedReservationIndex: 0,
    reservationsLoading: false,
    transactions: [],
    transactionsLoading: false,
    selectedTransactionIndex: 0,
    transactionsHasMore: false,
    transactionsNextCursor: null,
    transactionsCursorStack: [],
    transactionsTotal: null,
    integrityResult: null,
    policies: [],
    policiesLoading: false,
    budget: null,
    budgetLoading: false,
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

      usePaymentsStore.getState().setActiveTab("balance");
      expect(usePaymentsStore.getState().activeTab).toBe("balance");

      usePaymentsStore.getState().setActiveTab("transactions");
      expect(usePaymentsStore.getState().activeTab).toBe("transactions");

      usePaymentsStore.getState().setActiveTab("policies");
      expect(usePaymentsStore.getState().activeTab).toBe("policies");
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
  // setSelectedTransactionIndex
  // ---------------------------------------------------------------------------

  describe("setSelectedTransactionIndex", () => {
    it("sets the selected transaction index", () => {
      usePaymentsStore.getState().setSelectedTransactionIndex(5);
      expect(usePaymentsStore.getState().selectedTransactionIndex).toBe(5);
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
  // fetchTransactions
  // ---------------------------------------------------------------------------

  describe("fetchTransactions", () => {
    it("fetches and stores transaction records", async () => {
      const client = mockClient({
        "/api/v2/audit/transactions": {
          transactions: [
            {
              id: "tx-001",
              record_hash: "abc123",
              created_at: "2025-06-01T12:00:00Z",
              protocol: "credits",
              buyer_agent_id: "agent-1",
              seller_agent_id: "agent-2",
              amount: "100.00",
              currency: "USDC",
              status: "completed",
              zone_id: "zone-1",
              trace_id: "trace-001",
              metadata_hash: "meta123",
              transfer_id: "xfer-001",
            },
            {
              id: "tx-002",
              record_hash: "def456",
              created_at: "2025-06-01T13:00:00Z",
              protocol: "x402",
              buyer_agent_id: "agent-3",
              seller_agent_id: "agent-4",
              amount: "50.00",
              currency: "USDC",
              status: "pending",
              zone_id: "zone-1",
              trace_id: "trace-002",
              metadata_hash: "meta456",
              transfer_id: "xfer-002",
            },
          ],
          limit: 50,
          has_more: false,
          total: 2,
          next_cursor: null,
        },
      });

      await usePaymentsStore.getState().fetchTransactions(client);
      const state = usePaymentsStore.getState();

      expect(state.transactions).toHaveLength(2);
      expect(state.transactions[0]!.id).toBe("tx-001");
      expect(state.transactions[0]!.amount).toBe("100.00");
      expect(state.transactions[0]!.protocol).toBe("credits");
      expect(state.transactions[1]!.id).toBe("tx-002");
      expect(state.transactionsLoading).toBe(false);
      expect(state.transactionsHasMore).toBe(false);
      expect(state.transactionsNextCursor).toBeNull();
      expect(state.transactionsTotal).toBe(2);
      expect(state.error).toBeNull();
    });

    it("stores pagination cursor when has_more is true", async () => {
      const client = mockClient({
        "/api/v2/audit/transactions": {
          transactions: [
            {
              id: "tx-001",
              record_hash: "abc",
              created_at: "2025-06-01T12:00:00Z",
              protocol: "credits",
              buyer_agent_id: "a-1",
              seller_agent_id: "a-2",
              amount: "10.00",
              currency: "USDC",
              status: "completed",
              zone_id: "z-1",
              trace_id: null,
              metadata_hash: null,
              transfer_id: null,
            },
          ],
          limit: 50,
          has_more: true,
          total: 100,
          next_cursor: "cursor-abc",
        },
      });

      await usePaymentsStore.getState().fetchTransactions(client);
      const state = usePaymentsStore.getState();

      expect(state.transactionsHasMore).toBe(true);
      expect(state.transactionsNextCursor).toBe("cursor-abc");
      expect(state.transactionsTotal).toBe(100);
    });

    it("passes cursor param when provided", async () => {
      const client = mockClient({
        "/api/v2/audit/transactions": {
          transactions: [],
          limit: 50,
          has_more: false,
          total: 0,
          next_cursor: null,
        },
      });

      await usePaymentsStore.getState().fetchTransactions(client, "my-cursor");

      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("cursor=my-cursor");
    });

    it("clears cursor stack on first-page refresh (no cursor)", async () => {
      usePaymentsStore.setState({
        transactionsCursorStack: ["cursor-a", "cursor-b"],
      });

      const client = mockClient({
        "/api/v2/audit/transactions": {
          transactions: [],
          limit: 50,
          has_more: false,
          total: 0,
          next_cursor: null,
        },
      });

      // Refresh without cursor = first page
      await usePaymentsStore.getState().fetchTransactions(client);
      expect(usePaymentsStore.getState().transactionsCursorStack).toHaveLength(0);
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Audit service unavailable");
        }),
      } as unknown as FetchClient;

      await usePaymentsStore.getState().fetchTransactions(client);
      const state = usePaymentsStore.getState();

      expect(state.transactions).toHaveLength(0);
      expect(state.transactionsLoading).toBe(false);
      expect(state.error).toBe("Audit service unavailable");
    });
  });

  // ---------------------------------------------------------------------------
  // fetchPolicies
  // ---------------------------------------------------------------------------

  describe("fetchPolicies", () => {
    it("fetches and stores policy records", async () => {
      // Backend returns bare list[PolicyResponse], not { policies: [...] }
      const client = mockClient({
        "/api/v2/pay/policies": [
          {
            policy_id: "pol-001",
            zone_id: "zone-1",
            agent_id: "agent-1",
            daily_limit: "1000.00",
            weekly_limit: null,
            monthly_limit: null,
            per_tx_limit: "500.00",
            auto_approve_threshold: null,
            max_tx_per_hour: null,
            max_tx_per_day: 100,
            rules: null,
            priority: 1,
            enabled: true,
          },
        ],
      });

      await usePaymentsStore.getState().fetchPolicies(client);
      const state = usePaymentsStore.getState();

      expect(state.policies).toHaveLength(1);
      expect(state.policies[0]!.policy_id).toBe("pol-001");
      expect(state.policies[0]!.daily_limit).toBe("1000.00");
      expect(state.policies[0]!.weekly_limit).toBeNull();
      expect(state.policies[0]!.enabled).toBe(true);
      expect(state.policiesLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Policy service unavailable");
        }),
      } as unknown as FetchClient;

      await usePaymentsStore.getState().fetchPolicies(client);
      const state = usePaymentsStore.getState();

      expect(state.policies).toHaveLength(0);
      expect(state.policiesLoading).toBe(false);
      expect(state.error).toBe("Policy service unavailable");
    });
  });

  // ---------------------------------------------------------------------------
  // fetchBudget
  // ---------------------------------------------------------------------------

  describe("fetchBudget", () => {
    it("fetches and stores budget summary", async () => {
      const client = mockClient({
        "/api/v2/pay/budget": {
          has_policy: true,
          policy_id: "pol-001",
          limits: { daily: "1000.00", weekly: "5000.00", monthly: "20000.00" },
          spent: { daily: "200.00", weekly: "800.00", monthly: "3000.00" },
          remaining: { daily: "800.00", weekly: "4200.00", monthly: "17000.00" },
          rate_limits: null,
          has_rules: false,
        },
      });

      await usePaymentsStore.getState().fetchBudget(client);
      const state = usePaymentsStore.getState();

      expect(state.budget).not.toBeNull();
      expect(state.budget!.has_policy).toBe(true);
      expect(state.budget!.limits.daily).toBe("1000.00");
      expect(state.budget!.spent.daily).toBe("200.00");
      expect(state.budget!.remaining.daily).toBe("800.00");
      expect(state.budgetLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Budget service unavailable");
        }),
      } as unknown as FetchClient;

      await usePaymentsStore.getState().fetchBudget(client);
      const state = usePaymentsStore.getState();

      expect(state.budget).toBeNull();
      expect(state.budgetLoading).toBe(false);
      expect(state.error).toBe("Budget service unavailable");
    });
  });

  // ---------------------------------------------------------------------------
  // deletePolicy
  // ---------------------------------------------------------------------------

  describe("deletePolicy", () => {
    it("deletes policy and removes from local state", async () => {
      usePaymentsStore.setState({
        policies: [
          {
            policy_id: "pol-001",
            zone_id: "zone-1",
            agent_id: "agent-1",
            daily_limit: "1000.00",
            weekly_limit: "5000.00",
            monthly_limit: "20000.00",
            per_tx_limit: "500.00",
            auto_approve_threshold: "100.00",
            max_tx_per_hour: 10,
            max_tx_per_day: 100,
            rules: [],
            priority: 1,
            enabled: true,
          },
          {
            policy_id: "pol-002",
            zone_id: "zone-1",
            agent_id: "agent-2",
            daily_limit: "500.00",
            weekly_limit: "2500.00",
            monthly_limit: "10000.00",
            per_tx_limit: "250.00",
            auto_approve_threshold: "50.00",
            max_tx_per_hour: 5,
            max_tx_per_day: 50,
            rules: [],
            priority: 2,
            enabled: false,
          },
        ],
      });

      const client = mockClient({
        "/api/v2/pay/policies/pol-001": undefined,
      });

      await usePaymentsStore.getState().deletePolicy("pol-001", client);
      const state = usePaymentsStore.getState();

      expect(state.error).toBeNull();
      expect(state.policies).toHaveLength(1);
      expect(state.policies[0]!.policy_id).toBe("pol-002");
      expect(
        (client.deleteNoContent as ReturnType<typeof mock>).mock.calls.length,
      ).toBe(1);
    });

    it("sets error on failure", async () => {
      const client = {
        deleteNoContent: mock(async () => {
          throw new Error("Policy not found");
        }),
      } as unknown as FetchClient;

      await usePaymentsStore.getState().deletePolicy("pol-999", client);
      expect(usePaymentsStore.getState().error).toBe("Policy not found");
    });
  });

  // ---------------------------------------------------------------------------
  // fetchNextTransactions / fetchPrevTransactions
  // ---------------------------------------------------------------------------

  describe("fetchNextTransactions", () => {
    it("does nothing when there is no next cursor", async () => {
      usePaymentsStore.setState({
        transactionsHasMore: false,
        transactionsNextCursor: null,
      });

      const client = mockClient({});
      await usePaymentsStore.getState().fetchNextTransactions(client);

      // get should not have been called
      expect((client.get as ReturnType<typeof mock>).mock.calls.length).toBe(0);
    });

    it("fetches next page and pushes cursor onto stack", async () => {
      usePaymentsStore.setState({
        transactionsHasMore: true,
        transactionsNextCursor: "cursor-page2",
        transactionsCursorStack: [],
      });

      const client = mockClient({
        "/api/v2/audit/transactions": {
          transactions: [],
          limit: 50,
          has_more: false,
          total: 0,
          next_cursor: null,
        },
      });

      await usePaymentsStore.getState().fetchNextTransactions(client);
      const state = usePaymentsStore.getState();

      const calledUrl = (client.get as ReturnType<typeof mock>).mock.calls[0]![0] as string;
      expect(calledUrl).toContain("cursor=cursor-page2");
      expect(state.transactionsCursorStack).toHaveLength(1);
    });
  });

  describe("fetchPrevTransactions", () => {
    it("does nothing when cursor stack is empty", async () => {
      usePaymentsStore.setState({
        transactionsCursorStack: [],
      });

      const client = mockClient({});
      await usePaymentsStore.getState().fetchPrevTransactions(client);

      expect((client.get as ReturnType<typeof mock>).mock.calls.length).toBe(0);
    });

    it("pops cursor stack and fetches previous page", async () => {
      usePaymentsStore.setState({
        transactionsCursorStack: ["cursor-page2"],
      });

      const client = mockClient({
        "/api/v2/audit/transactions": {
          transactions: [],
          limit: 50,
          has_more: true,
          total: 10,
          next_cursor: "cursor-page2",
        },
      });

      await usePaymentsStore.getState().fetchPrevTransactions(client);
      const state = usePaymentsStore.getState();

      // Stack should be empty after going back to first page
      expect(state.transactionsCursorStack).toHaveLength(0);
    });
  });

  // ---------------------------------------------------------------------------
  // verifyIntegrity
  // ---------------------------------------------------------------------------

  describe("verifyIntegrity", () => {
    it("returns integrity result on success", async () => {
      const client = mockClient({
        "/api/v2/audit/integrity/tx-001": {
          record_id: "tx-001",
          is_valid: true,
          record_hash: "abc123",
        },
      });

      const result = await usePaymentsStore
        .getState()
        .verifyIntegrity("tx-001", client);
      const state = usePaymentsStore.getState();

      expect(state.error).toBeNull();
      expect(result).not.toBeNull();
      expect(result!.record_id).toBe("tx-001");
      expect(result!.is_valid).toBe(true);
      expect(result!.record_hash).toBe("abc123");
    });

    it("returns null and sets error on failure", async () => {
      const client = {
        get: mock(async () => {
          throw new Error("Integrity check failed");
        }),
      } as unknown as FetchClient;

      const result = await usePaymentsStore
        .getState()
        .verifyIntegrity("tx-999", client);
      const state = usePaymentsStore.getState();

      expect(result).toBeNull();
      expect(state.error).toBe("Integrity check failed");
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

    it("fetchTransactions clears previous error on start", async () => {
      usePaymentsStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/audit/transactions": {
          transactions: [],
          limit: 50,
          has_more: false,
          total: 0,
          next_cursor: null,
        },
      });

      await usePaymentsStore.getState().fetchTransactions(client);
      expect(usePaymentsStore.getState().error).toBeNull();
    });

    it("fetchPolicies clears previous error on start", async () => {
      usePaymentsStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/pay/policies": { policies: [] },
      });

      await usePaymentsStore.getState().fetchPolicies(client);
      expect(usePaymentsStore.getState().error).toBeNull();
    });

    it("fetchBudget clears previous error on start", async () => {
      usePaymentsStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/pay/budget": {
          has_policy: false,
          policy_id: null,
          limits: { daily: "0", weekly: "0", monthly: "0" },
          spent: { daily: "0", weekly: "0", monthly: "0" },
          remaining: { daily: "0", weekly: "0", monthly: "0" },
          rate_limits: null,
          has_rules: false,
        },
      });

      await usePaymentsStore.getState().fetchBudget(client);
      expect(usePaymentsStore.getState().error).toBeNull();
    });
  });
});
