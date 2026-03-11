/**
 * Zustand store for Payments & Credits panel.
 *
 * Manages balance queries, credit transfers, and reservations (hold/commit/release).
 *
 * Reservations are tracked locally (from createReservation responses) because
 * the backend has no reservation list endpoint.
 *
 * Note: The backend pay surface (pay.py) does not include /policies or /audit
 * endpoints. Those features are deferred until the backend exposes them.
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

export type PaymentsTab = "balance" | "reservations";

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
  readonly setActiveTab: (tab: PaymentsTab) => void;
  readonly setSelectedReservationIndex: (index: number) => void;
}

export const usePaymentsStore = create<PaymentsState>((set, get) => ({
  balance: null,
  balanceLoading: false,
  reservations: [],
  selectedReservationIndex: 0,
  reservationsLoading: false,
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

  setActiveTab: (tab) => {
    set({ activeTab: tab, error: null });
  },

  setSelectedReservationIndex: (index) => {
    set({ selectedReservationIndex: index });
  },
}));
