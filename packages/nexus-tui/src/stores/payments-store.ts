/**
 * Zustand store for Payments & Credits panel.
 *
 * Manages balance queries, credit transfers, reservations (hold/commit/release),
 * payment policies, and the audit log.
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
  readonly account_id: string;
  readonly available: string;
  readonly pending: string;
  readonly reserved: string;
  readonly currency: string;
  readonly updated_at: string;
}

export interface TransferResult {
  readonly transfer_id: string;
  readonly from_account: string;
  readonly to_account: string;
  readonly amount: string;
  readonly status: "completed" | "pending" | "failed";
  readonly created_at: string;
}

export interface Reservation {
  readonly reservation_id: string;
  readonly account_id: string;
  readonly amount: string;
  readonly status: "active" | "committed" | "released" | "expired";
  readonly description: string | null;
  readonly created_at: string;
  readonly expires_at: string;
}

export interface PaymentPolicy {
  readonly policy_id: string;
  readonly name: string;
  readonly type: string;
  readonly limit_amount: string | null;
  readonly period: string | null;
  readonly enabled: boolean;
}

export interface AuditEntry {
  readonly entry_id: string;
  readonly type: string;
  readonly amount: string;
  readonly from_account: string | null;
  readonly to_account: string | null;
  readonly status: string;
  readonly created_at: string;
  readonly description: string | null;
}

export type PaymentsTab = "balance" | "reservations" | "policies" | "audit";

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

  // Policies
  readonly policies: readonly PaymentPolicy[];
  readonly policiesLoading: boolean;

  // Audit
  readonly auditEntries: readonly AuditEntry[];
  readonly auditTotal: number;
  readonly auditLoading: boolean;

  // UI state
  readonly activeTab: PaymentsTab;
  readonly error: string | null;

  // Actions
  readonly fetchBalance: (client: FetchClient) => Promise<void>;
  readonly transfer: (
    toAccount: string,
    amount: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly createReservation: (
    amount: string,
    description: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly commitReservation: (id: string, client: FetchClient) => Promise<void>;
  readonly releaseReservation: (id: string, client: FetchClient) => Promise<void>;
  readonly fetchPolicies: (client: FetchClient) => Promise<void>;
  readonly fetchAudit: (client: FetchClient) => Promise<void>;
  readonly setActiveTab: (tab: PaymentsTab) => void;
  readonly setSelectedReservationIndex: (index: number) => void;
}

export const usePaymentsStore = create<PaymentsState>((set, get) => ({
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

  transfer: async (toAccount, amount, client) => {
    set({ error: null });

    try {
      await client.post<TransferResult>("/api/v2/pay/transfer", {
        to_account: toAccount,
        amount,
      });
      await get().fetchBalance(client);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to transfer credits",
      });
    }
  },

  createReservation: async (amount, description, client) => {
    set({ error: null });

    try {
      const reservation = await client.post<Reservation>("/api/v2/pay/reserve", {
        amount,
        description,
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
          r.reservation_id === id ? { ...r, status: "committed" as const } : r,
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
          r.reservation_id === id ? { ...r, status: "released" as const } : r,
        ),
      }));
    } catch (err) {
      set({
        error:
          err instanceof Error ? err.message : "Failed to release reservation",
      });
    }
  },

  fetchPolicies: async (client) => {
    set({ policiesLoading: true, error: null });

    try {
      const response = await client.get<{
        readonly policies: readonly PaymentPolicy[];
      }>("/api/v2/pay/policies");
      const policies = response.policies ?? [];
      set({ policies, policiesLoading: false });
    } catch (err) {
      set({
        policiesLoading: false,
        error:
          err instanceof Error ? err.message : "Failed to fetch policies",
      });
    }
  },

  fetchAudit: async (client) => {
    set({ auditLoading: true, error: null });

    try {
      const response = await client.get<{
        readonly transactions: readonly AuditEntry[];
        readonly total: number;
      }>("/api/v2/audit/transactions");
      set({
        auditEntries: response.transactions ?? [],
        auditTotal: response.total ?? 0,
        auditLoading: false,
      });
    } catch (err) {
      set({
        auditLoading: false,
        error:
          err instanceof Error
            ? err.message
            : "Failed to fetch audit log",
      });
    }
  },

  setActiveTab: (tab) => {
    set({ activeTab: tab, error: null });
  },

  setSelectedReservationIndex: (index) => {
    set({ selectedReservationIndex: index });
  },
}));
