/**
 * Zustand store for Versions & Snapshots panel.
 *
 * Manages transaction lifecycle (list, create, commit, rollback)
 * and per-transaction snapshot entries.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

// =============================================================================
// Types (snake_case matching API wire format)
// =============================================================================

export interface Transaction {
  readonly transaction_id: string;
  readonly zone_id: string;
  readonly agent_id: string | null;
  readonly status: "active" | "committed" | "rolled_back" | "expired";
  readonly description: string | null;
  readonly created_at: string;
  readonly expires_at: string;
  readonly entry_count: number;
}

export interface SnapshotEntry {
  readonly entry_id: string;
  readonly transaction_id: string;
  readonly path: string;
  readonly operation: "write" | "delete" | "rename";
  readonly original_hash: string | null;
  readonly new_hash: string | null;
  readonly created_at: string;
}

interface TransactionListResponse {
  readonly transactions: readonly Transaction[];
  readonly count: number;
}

export interface ConflictItem {
  readonly path: string;
  readonly reason: string;
  readonly expected_hash: string | null;
  readonly current_hash: string | null;
  readonly transaction_id: string | null;
}

interface ConflictsResponse {
  readonly conflicts: readonly ConflictItem[];
}

// =============================================================================
// Status filter cycle
// =============================================================================

const STATUS_CYCLE: readonly (string | null)[] = [
  null,
  "active",
  "committed",
  "rolled_back",
  "expired",
];

export function nextStatusFilter(current: string | null): string | null {
  const index = STATUS_CYCLE.indexOf(current);
  const next = (index + 1) % STATUS_CYCLE.length;
  return STATUS_CYCLE[next] ?? null;
}

// =============================================================================
// Store
// =============================================================================

export interface DiffContent {
  readonly old: string;
  readonly new: string;
}

export interface VersionsState {
  // Transaction list
  readonly transactions: readonly Transaction[];
  readonly selectedTransaction: Transaction | null;
  readonly selectedIndex: number;
  readonly statusFilter: string | null;
  readonly isLoading: boolean;
  readonly error: string | null;

  // Entries for selected transaction
  readonly entries: readonly SnapshotEntry[];
  readonly entriesLoading: boolean;

  // Diff viewer
  readonly diffContent: DiffContent | null;
  readonly diffLoading: boolean;

  // Transaction detail
  readonly transactionDetail: Transaction | null;
  readonly transactionDetailLoading: boolean;

  // Conflicts
  readonly conflicts: readonly ConflictItem[];
  readonly conflictsLoading: boolean;
  readonly showConflicts: boolean;

  // Actions
  readonly fetchTransactions: (client: FetchClient) => Promise<void>;
  readonly selectTransaction: (txn: Transaction) => void;
  readonly setSelectedIndex: (index: number) => void;
  readonly setStatusFilter: (status: string | null) => void;
  readonly fetchEntries: (txnId: string, client: FetchClient) => Promise<void>;
  readonly fetchDiff: (
    path: string,
    version1: string,
    version2: string,
    client: FetchClient,
  ) => Promise<void>;
  readonly fetchTransactionDetail: (txnId: string, client: FetchClient) => Promise<void>;
  readonly beginTransaction: (
    client: FetchClient,
    description?: string,
    ttlSeconds?: number,
  ) => Promise<void>;
  readonly commitTransaction: (txnId: string, client: FetchClient) => Promise<void>;
  readonly rollbackTransaction: (txnId: string, client: FetchClient) => Promise<void>;
  readonly fetchConflicts: (client: FetchClient) => Promise<void>;
  readonly toggleConflicts: () => void;
}

export const useVersionsStore = create<VersionsState>((set, get) => ({
  transactions: [],
  selectedTransaction: null,
  selectedIndex: 0,
  statusFilter: null,
  isLoading: false,
  error: null,
  entries: [],
  entriesLoading: false,
  diffContent: null,
  diffLoading: false,
  transactionDetail: null,
  transactionDetailLoading: false,
  conflicts: [],
  conflictsLoading: false,
  showConflicts: false,

  fetchTransactions: async (client) => {
    set({ isLoading: true, error: null });

    try {
      const { statusFilter } = get();
      const query = statusFilter ? `?status=${encodeURIComponent(statusFilter)}` : "";
      const response = await client.get<TransactionListResponse>(
        `/api/v2/snapshots${query}`,
      );

      const transactions = response.transactions ?? [];
      set({ transactions, isLoading: false });
    } catch (err) {
      set({
        isLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch transactions",
      });
    }
  },

  selectTransaction: (txn) => {
    const { transactions } = get();
    const index = transactions.findIndex(
      (t) => t.transaction_id === txn.transaction_id,
    );
    set({
      selectedTransaction: txn,
      selectedIndex: index >= 0 ? index : 0,
      entries: [],
    });
  },

  setSelectedIndex: (index) => {
    const { transactions } = get();
    const txn = transactions[index] ?? null;
    set({ selectedIndex: index, selectedTransaction: txn });
  },

  setStatusFilter: (status) => {
    set({ statusFilter: status, selectedIndex: 0, selectedTransaction: null });
  },

  fetchEntries: async (txnId, client) => {
    set({ entriesLoading: true });

    try {
      const entries = await client.get<readonly SnapshotEntry[]>(
        `/api/v2/snapshots/${encodeURIComponent(txnId)}/entries`,
      );
      set({ entries: entries ?? [], entriesLoading: false });
    } catch (err) {
      set({
        entries: [],
        entriesLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch entries",
      });
    }
  },

  fetchDiff: async (path, version1, version2, client) => {
    set({ diffLoading: true, diffContent: null, error: null });

    try {
      const [oldResponse, newResponse] = await Promise.all([
        client.get<{ content: string }>(
          `/api/v2/files/read?path=${encodeURIComponent(path)}&version=${encodeURIComponent(version1)}&include_metadata=false`,
        ),
        client.get<{ content: string }>(
          `/api/v2/files/read?path=${encodeURIComponent(path)}&version=${encodeURIComponent(version2)}&include_metadata=false`,
        ),
      ]);

      set({
        diffContent: {
          old: oldResponse.content ?? "",
          new: newResponse.content ?? "",
        },
        diffLoading: false,
      });
    } catch (err) {
      set({
        diffContent: null,
        diffLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch diff",
      });
    }
  },

  fetchTransactionDetail: async (txnId, client) => {
    set({ transactionDetailLoading: true, error: null });
    try {
      const detail = await client.get<Transaction>(
        `/api/v2/snapshots/${encodeURIComponent(txnId)}`,
      );
      set({ transactionDetail: detail, transactionDetailLoading: false });
    } catch (err) {
      set({
        transactionDetailLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch transaction detail",
      });
    }
  },

  beginTransaction: async (client, description, ttlSeconds) => {
    set({ error: null });

    try {
      const body: Record<string, unknown> = {};
      if (description !== undefined) body["description"] = description;
      if (ttlSeconds !== undefined) body["ttl_seconds"] = ttlSeconds;

      await client.post<Transaction>("/api/v2/snapshots", body);
      await get().fetchTransactions(client);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to begin transaction",
      });
    }
  },

  commitTransaction: async (txnId, client) => {
    set({ error: null });

    try {
      await client.post<Transaction>(
        `/api/v2/snapshots/${encodeURIComponent(txnId)}/commit`,
        {},
      );
      await get().fetchTransactions(client);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to commit transaction",
      });
    }
  },

  rollbackTransaction: async (txnId, client) => {
    set({ error: null });

    try {
      await client.post<Transaction>(
        `/api/v2/snapshots/${encodeURIComponent(txnId)}/rollback`,
        {},
      );
      await get().fetchTransactions(client);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to rollback transaction",
      });
    }
  },

  fetchConflicts: async (client) => {
    set({ conflictsLoading: true, error: null });

    try {
      const response = await client.get<ConflictsResponse>(
        "/api/v2/sync/conflicts",
      );
      set({
        conflicts: response.conflicts ?? [],
        conflictsLoading: false,
      });
    } catch (err) {
      set({
        conflicts: [],
        conflictsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch conflicts",
      });
    }
  },

  toggleConflicts: () => {
    set((state) => ({ showConflicts: !state.showConflicts }));
  },
}));
