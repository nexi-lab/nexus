/**
 * Zustand store for upload session management.
 *
 * Tracks active upload sessions with progress tracking and lifecycle actions.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

// =============================================================================
// Types
// =============================================================================

export interface UploadSession {
  readonly id: string;
  readonly filename: string | null;
  readonly offset: number;
  readonly length: number;
  readonly expires_at: string | null;
  readonly created_at: string;
}

// =============================================================================
// Store
// =============================================================================

export interface UploadState {
  readonly sessions: readonly UploadSession[];
  readonly sessionsLoading: boolean;
  readonly selectedSessionIndex: number;
  readonly error: string | null;

  readonly fetchSessions: (client: FetchClient) => Promise<void>;
  readonly terminateSession: (sessionId: string, client: FetchClient) => Promise<void>;
  readonly refreshSessionStatus: (sessionId: string, client: FetchClient) => Promise<void>;
  readonly setSelectedSessionIndex: (index: number) => void;
}

export const useUploadStore = create<UploadState>((set, get) => ({
  sessions: [],
  sessionsLoading: false,
  selectedSessionIndex: 0,
  error: null,

  fetchSessions: async (client) => {
    set({ sessionsLoading: true, error: null });
    try {
      const response = await client.get<{
        readonly sessions: readonly UploadSession[];
      }>("/api/v2/uploads");
      set({
        sessions: response.sessions ?? [],
        sessionsLoading: false,
        selectedSessionIndex: 0,
      });
    } catch (err) {
      set({
        sessionsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch upload sessions",
      });
    }
  },

  terminateSession: async (sessionId, client) => {
    set({ error: null });
    try {
      await client.delete(`/api/v2/uploads/${encodeURIComponent(sessionId)}`);
      set((state) => ({
        sessions: state.sessions.filter((s) => s.id !== sessionId),
      }));
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to terminate upload session",
      });
    }
  },

  refreshSessionStatus: async (sessionId, client) => {
    set({ error: null });
    try {
      const response = await client.get<UploadSession>(
        `/api/v2/uploads/${encodeURIComponent(sessionId)}`,
      );
      set((state) => ({
        sessions: state.sessions.map((s) =>
          s.id === sessionId ? response : s,
        ),
      }));
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to refresh session status",
      });
    }
  },

  setSelectedSessionIndex: (index) => {
    set({ selectedSessionIndex: index });
  },
}));
