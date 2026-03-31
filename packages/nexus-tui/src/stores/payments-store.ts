/**
 * Zustand store for Payments & Credits panel.
 *
 * Manages balance queries, credit transfers, reservations (hold/commit/release),
 * transaction audit feed, and spending policies / budget.
 *
 * Reservations are tracked locally (from createReservation responses) because
 * the backend has no reservation list endpoint.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";
import { createApiAction, categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";
import { useUiStore } from "./ui-store.js";

// =============================================================================
// Types (snake_case matching API wire format)
// =============================================================================

export interface BalanceInfo {
  readonly available: string;
  readonly reserved: string;
  readonly total: string;
}

/** Matches backend ReceiptResponse from pay.py. */
export interface TransferReceipt {
  readonly id: string;
  readonly method: string;
  readonly amount: string;
  readonly from_agent: string;
  readonly to_agent: string;
  readonly memo: string | null;
  readonly timestamp: string | null;
  readonly tx_hash: string | null;
}

/** Matches backend ReservationResponse from pay.py. */
export interface Reservation {
  readonly id: string;
  readonly amount: string;
  readonly purpose: string;
  readonly expires_at: string | null;
  readonly status: "pending" | "committed" | "released";
}

/** Matches backend audit transaction record. */
export interface TransactionRecord {
  readonly id: string;
  readonly record_hash: string;
  readonly created_at: string;
  readonly protocol: string;
  readonly buyer_agent_id: string;
  readonly seller_agent_id: string;
  readonly amount: string;
  readonly currency: string;
  readonly status: string;
  readonly zone_id: string;
  readonly trace_id: string | null;
  readonly metadata_hash: string | null;
  readonly transfer_id: string | null;
}

/** Matches backend PolicyResponse from pay.py:541. All limit fields are nullable. */
export interface PolicyRecord {
  readonly policy_id: string;
  readonly zone_id: string;
  readonly agent_id: string | null;
  readonly daily_limit: string | null;
  readonly weekly_limit: string | null;
  readonly monthly_limit: string | null;
  readonly per_tx_limit: string | null;
  readonly auto_approve_threshold: string | null;
  readonly max_tx_per_hour: number | null;
  readonly max_tx_per_day: number | null;
  readonly rules: readonly unknown[] | null;
  readonly priority: number;
  readonly enabled: boolean;
}

/** Matches backend budget summary. */
export interface BudgetSummary {
  readonly has_policy: boolean;
  readonly policy_id: string | null;
  readonly limits: {
    readonly daily: string;
    readonly weekly: string;
    readonly monthly: string;
  };
  readonly spent: {
    readonly daily: string;
    readonly weekly: string;
    readonly monthly: string;
  };
  readonly remaining: {
    readonly daily: string;
    readonly weekly: string;
    readonly monthly: string;
  };
  readonly rate_limits: unknown;
  readonly has_rules: boolean;
}

/** Integrity verification result. */
export interface IntegrityResult {
  readonly record_id: string;
  readonly is_valid: boolean;
  readonly record_hash: string;
}

/** Matches backend spending approval request. */
export interface ApprovalRequest {
  readonly id: string;
  readonly requester_id: string;
  readonly amount: number;
  readonly purpose: string;
  readonly status: "pending" | "approved" | "rejected";
  readonly created_at: string;
  readonly decided_at: string | null;
  readonly decided_by: string | null;
}

export type PaymentsTab = "balance" | "reservations" | "transactions" | "policies" | "approvals";

// =============================================================================
// Wire response shapes
// =============================================================================

interface TransactionsResponse {
  readonly transactions: readonly TransactionRecord[];
  readonly limit: number;
  readonly has_more: boolean;
  readonly total: number;
  readonly next_cursor: string | null;
}

// Note: GET /api/v2/pay/policies returns a bare list[PolicyResponse] (pay.py:641),
// not a wrapper object. We type the response as PolicyRecord[] directly.

// =============================================================================
// Store
// =============================================================================

export interface PaymentsState {
  // Balance
  readonly balance: BalanceInfo | null;
  readonly balanceLoading: boolean;

  // Reservations (tracked locally from create responses)
  readonly reservations: readonly Reservation[];
  readonly selectedReservationIndex: number;
  readonly reservationsLoading: boolean;

  // Transactions (audit feed with cursor-based pagination)
  readonly transactions: readonly TransactionRecord[];
  readonly transactionsLoading: boolean;
  readonly selectedTransactionIndex: number;
  readonly transactionsHasMore: boolean;
  readonly transactionsNextCursor: string | null;
  readonly transactionsCursorStack: readonly string[];
  readonly transactionsTotal: number | null;
  readonly integrityResult: IntegrityResult | null;

  // Policies & budget
  readonly policies: readonly PolicyRecord[];
  readonly policiesLoading: boolean;
  readonly budget: BudgetSummary | null;
  readonly budgetLoading: boolean;

  // Affordability check
  readonly affordResult: { can_afford: boolean; balance: string; requested: string } | null;

  // Approvals
  readonly approvals: readonly ApprovalRequest[];
  readonly approvalsLoading: boolean;
  readonly selectedApprovalIndex: number;

  // UI state
  readonly activeTab: PaymentsTab;
  readonly error: string | null;

  // Actions
  readonly fetchBalance: (client: FetchClient) => Promise<void>;
  readonly transfer: (
    to: string,
    amount: string,
    memo: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly createReservation: (
    amount: string,
    purpose: string,
    timeout: number,
    client: FetchClient,
  ) => Promise<void>;
  readonly commitReservation: (id: string, client: FetchClient) => Promise<void>;
  readonly releaseReservation: (id: string, client: FetchClient) => Promise<void>;
  readonly fetchTransactions: (client: FetchClient, cursor?: string) => Promise<void>;
  readonly fetchNextTransactions: (client: FetchClient) => Promise<void>;
  readonly fetchPrevTransactions: (client: FetchClient) => Promise<void>;
  readonly fetchPolicies: (client: FetchClient) => Promise<void>;
  readonly fetchBudget: (client: FetchClient) => Promise<void>;
  readonly deletePolicy: (policyId: string, client: FetchClient) => Promise<void>;
  readonly verifyIntegrity: (recordId: string, client: FetchClient) => Promise<IntegrityResult | null>;
  readonly createPolicy: (name: string, rules: Record<string, unknown>, client: FetchClient) => Promise<void>;
  readonly checkAfford: (amount: string, client: FetchClient) => Promise<void>;
  readonly fetchApprovals: (client: FetchClient) => Promise<void>;
  readonly requestApproval: (amount: number, purpose: string, client: FetchClient) => Promise<void>;
  readonly approveRequest: (approvalId: string, client: FetchClient) => Promise<void>;
  readonly rejectRequest: (approvalId: string, client: FetchClient) => Promise<void>;
  readonly setSelectedApprovalIndex: (index: number) => void;
  readonly setActiveTab: (tab: PaymentsTab) => void;
  readonly setSelectedReservationIndex: (index: number) => void;
  readonly setSelectedTransactionIndex: (index: number) => void;
}

const SOURCE = "payments";

export const usePaymentsStore = create<PaymentsState>((set, get) => ({
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
  affordResult: null,
  approvals: [],
  approvalsLoading: false,
  selectedApprovalIndex: 0,
  activeTab: "balance",
  error: null,

  // =========================================================================
  // Actions migrated to createApiAction
  // =========================================================================

  fetchBalance: createApiAction<PaymentsState, [FetchClient]>(set, {
    loadingKey: "balanceLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch balance",
    action: async (client) => {
      const balance = await client.get<BalanceInfo>("/api/v2/pay/balance");
      return { balance: balance ?? null };
    },
  }),

  fetchPolicies: createApiAction<PaymentsState, [FetchClient]>(set, {
    loadingKey: "policiesLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch policies",
    action: async (client) => {
      // Backend returns bare list[PolicyResponse], not a wrapper object
      const policies = await client.get<readonly PolicyRecord[]>("/api/v2/pay/policies");
      return { policies };
    },
  }),

  fetchBudget: createApiAction<PaymentsState, [FetchClient]>(set, {
    loadingKey: "budgetLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch budget",
    action: async (client) => {
      const budget = await client.get<BudgetSummary>("/api/v2/pay/budget");
      return { budget: budget ?? null };
    },
  }),

  // =========================================================================
  // Actions without loading keys or with special patterns — inline with error store
  // =========================================================================

  transfer: async (to, amount, memo, client) => {
    set({ error: null });

    try {
      await client.post<TransferReceipt>("/api/v2/pay/transfer", {
        to,
        amount,
        memo,
      });
      await get().fetchBalance(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to transfer credits";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  createReservation: async (amount, purpose, timeout, client) => {
    set({ error: null });

    try {
      const reservation = await client.post<Reservation>("/api/v2/pay/reserve", {
        amount,
        purpose,
        timeout,
      });
      set((state) => ({
        reservations: [...state.reservations, reservation],
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to create reservation";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  commitReservation: async (id, client) => {
    set({ error: null });

    try {
      await client.postNoContent(
        `/api/v2/pay/reserve/${encodeURIComponent(id)}/commit`,
      );
      set((state) => ({
        reservations: state.reservations.map((r) =>
          r.id === id ? { ...r, status: "committed" as const } : r,
        ),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to commit reservation";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  releaseReservation: async (id, client) => {
    set({ error: null });

    try {
      await client.postNoContent(
        `/api/v2/pay/reserve/${encodeURIComponent(id)}/release`,
      );
      set((state) => ({
        reservations: state.reservations.map((r) =>
          r.id === id ? { ...r, status: "released" as const } : r,
        ),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to release reservation";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchTransactions: async (client, cursor) => {
    // Reset pagination state when fetching first page (no cursor = fresh load)
    const resetPagination = !cursor ? { transactionsCursorStack: [] as readonly string[] } : {};
    set({ transactionsLoading: true, error: null, ...resetPagination });

    try {
      const params = new URLSearchParams({ limit: "50", include_total: "true" });
      if (cursor) {
        params.set("cursor", cursor);
      }
      const data = await client.get<TransactionsResponse>(
        `/api/v2/audit/transactions?${params.toString()}`,
      );
      set({
        transactions: data.transactions,
        transactionsHasMore: data.has_more,
        transactionsNextCursor: data.next_cursor ?? null,
        transactionsTotal: data.total ?? null,
        selectedTransactionIndex: 0,
        transactionsLoading: false,
        integrityResult: null,
      });
      useUiStore.getState().markDataUpdated("payments");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch transactions";
      set({
        transactionsLoading: false,
        error: message,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchNextTransactions: async (client) => {
    const { transactionsNextCursor, transactionsHasMore } = get();
    if (!transactionsHasMore || !transactionsNextCursor) return;

    // Push current page's next_cursor onto stack before navigating forward
    set((state) => ({
      transactionsCursorStack: [...state.transactionsCursorStack, transactionsNextCursor],
    }));

    await get().fetchTransactions(client, transactionsNextCursor);
  },

  fetchPrevTransactions: async (client) => {
    const { transactionsCursorStack } = get();
    if (transactionsCursorStack.length === 0) return;

    // Pop the current cursor (it brought us to this page)
    const stack = [...transactionsCursorStack];
    stack.pop();

    // The previous cursor (or undefined for first page)
    const prevCursor = stack.length > 0 ? stack[stack.length - 1] : undefined;
    set({ transactionsCursorStack: stack });

    await get().fetchTransactions(client, prevCursor);
  },

  deletePolicy: async (policyId, client) => {
    set({ error: null });

    try {
      await client.deleteNoContent(
        `/api/v2/pay/policies/${encodeURIComponent(policyId)}`,
      );
      set((state) => ({
        policies: state.policies.filter((p) => p.policy_id !== policyId),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to delete policy";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  verifyIntegrity: async (recordId, client) => {
    set({ error: null, integrityResult: null });

    try {
      const result = await client.get<IntegrityResult>(
        `/api/v2/audit/integrity/${encodeURIComponent(recordId)}`,
      );
      set({ integrityResult: result });
      useUiStore.getState().markDataUpdated("payments");
      return result;
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to verify integrity";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
      return null;
    }
  },

  createPolicy: async (name, rules, client) => {
    set({ policiesLoading: true, error: null });
    try {
      await client.post("/api/v2/pay/policies", { name, rules });
      await get().fetchPolicies(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to create policy";
      set({ policiesLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  checkAfford: async (amount, client) => {
    set({ error: null });
    try {
      const result = await client.get<{ can_afford: boolean; balance: string; requested: string }>(
        `/api/v2/pay/can-afford?amount=${encodeURIComponent(amount)}`,
      );
      set({ affordResult: result });
      useUiStore.getState().markDataUpdated("payments");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to check affordability";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  // =========================================================================
  // Approvals
  // =========================================================================

  fetchApprovals: createApiAction<PaymentsState, [FetchClient]>(set, {
    loadingKey: "approvalsLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch approvals",
    action: async (client) => {
      const approvals = await client.get<readonly ApprovalRequest[]>("/api/v2/pay/approvals");
      return { approvals, selectedApprovalIndex: 0 };
    },
  }),

  requestApproval: async (amount, purpose, client) => {
    set({ approvalsLoading: true, error: null });
    try {
      await client.post("/api/v2/pay/approvals/request", { amount, purpose });
      await get().fetchApprovals(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to request approval";
      set({ approvalsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  approveRequest: async (approvalId, client) => {
    set({ approvalsLoading: true, error: null });
    try {
      await client.post(`/api/v2/pay/approvals/${encodeURIComponent(approvalId)}/approve`, {});
      await get().fetchApprovals(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to approve request";
      set({ approvalsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  rejectRequest: async (approvalId, client) => {
    set({ approvalsLoading: true, error: null });
    try {
      await client.post(`/api/v2/pay/approvals/${encodeURIComponent(approvalId)}/reject`, {});
      await get().fetchApprovals(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to reject request";
      set({ approvalsLoading: false, error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  setSelectedApprovalIndex: (index) => {
    set({ selectedApprovalIndex: index });
  },

  setActiveTab: (tab) => {
    set({ activeTab: tab, error: null });
  },

  setSelectedReservationIndex: (index) => {
    set({ selectedReservationIndex: index });
  },

  setSelectedTransactionIndex: (index) => {
    set({ selectedTransactionIndex: index });
  },
}));
