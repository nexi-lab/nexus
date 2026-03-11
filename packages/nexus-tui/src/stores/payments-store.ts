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
  readonly trace_id: string;
  readonly metadata_hash: string;
  readonly transfer_id: string;
}

/** Matches backend policy record. */
export interface PolicyRecord {
  readonly policy_id: string;
  readonly zone_id: string;
  readonly agent_id: string;
  readonly daily_limit: string;
  readonly weekly_limit: string;
  readonly monthly_limit: string;
  readonly per_tx_limit: string;
  readonly auto_approve_threshold: string;
  readonly max_tx_per_hour: number;
  readonly max_tx_per_day: number;
  readonly rules: readonly unknown[];
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

export type PaymentsTab = "balance" | "reservations" | "transactions" | "policies";

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

interface PoliciesResponse {
  readonly policies: readonly PolicyRecord[];
}

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

  // Transactions (audit feed)
  readonly transactions: readonly TransactionRecord[];
  readonly transactionsLoading: boolean;
  readonly selectedTransactionIndex: number;

  // Policies & budget
  readonly policies: readonly PolicyRecord[];
  readonly policiesLoading: boolean;
  readonly budget: BudgetSummary | null;
  readonly budgetLoading: boolean;

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
  readonly fetchTransactions: (client: FetchClient) => Promise<void>;
  readonly fetchPolicies: (client: FetchClient) => Promise<void>;
  readonly fetchBudget: (client: FetchClient) => Promise<void>;
  readonly deletePolicy: (policyId: string, client: FetchClient) => Promise<void>;
  readonly verifyIntegrity: (recordId: string, client: FetchClient) => Promise<IntegrityResult | null>;
  readonly setActiveTab: (tab: PaymentsTab) => void;
  readonly setSelectedReservationIndex: (index: number) => void;
  readonly setSelectedTransactionIndex: (index: number) => void;
}

export const usePaymentsStore = create<PaymentsState>((set, get) => ({
  balance: null,
  balanceLoading: false,
  reservations: [],
  selectedReservationIndex: 0,
  reservationsLoading: false,
  transactions: [],
  transactionsLoading: false,
  selectedTransactionIndex: 0,
  policies: [],
  policiesLoading: false,
  budget: null,
  budgetLoading: false,
  activeTab: "balance",
  error: null,

  fetchBalance: async (client) => {
    set({ balanceLoading: true, error: null });

    try {
      const balance = await client.get<BalanceInfo>("/api/v2/pay/balance");
      set({ balance: balance ?? null, balanceLoading: false });
    } catch (err) {
      set({
        balanceLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch balance",
      });
    }
  },

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
      set({
        error: err instanceof Error ? err.message : "Failed to transfer credits",
      });
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
      set({
        error:
          err instanceof Error ? err.message : "Failed to create reservation",
      });
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
      set({
        error:
          err instanceof Error ? err.message : "Failed to commit reservation",
      });
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
      set({
        error:
          err instanceof Error ? err.message : "Failed to release reservation",
      });
    }
  },

  fetchTransactions: async (client) => {
    set({ transactionsLoading: true, error: null });

    try {
      const data = await client.get<TransactionsResponse>(
        "/api/v2/audit/transactions",
      );
      set({
        transactions: data.transactions,
        transactionsLoading: false,
      });
    } catch (err) {
      set({
        transactionsLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch transactions",
      });
    }
  },

  fetchPolicies: async (client) => {
    set({ policiesLoading: true, error: null });

    try {
      const data = await client.get<PoliciesResponse>("/api/v2/pay/policies");
      set({
        policies: data.policies,
        policiesLoading: false,
      });
    } catch (err) {
      set({
        policiesLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch policies",
      });
    }
  },

  fetchBudget: async (client) => {
    set({ budgetLoading: true, error: null });

    try {
      const budget = await client.get<BudgetSummary>("/api/v2/pay/budget");
      set({ budget: budget ?? null, budgetLoading: false });
    } catch (err) {
      set({
        budgetLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch budget",
      });
    }
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
      set({
        error:
          err instanceof Error ? err.message : "Failed to delete policy",
      });
    }
  },

  verifyIntegrity: async (recordId, client) => {
    set({ error: null });

    try {
      const result = await client.get<IntegrityResult>(
        `/api/v2/audit/integrity/${encodeURIComponent(recordId)}`,
      );
      return result;
    } catch (err) {
      set({
        error:
          err instanceof Error ? err.message : "Failed to verify integrity",
      });
      return null;
    }
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
