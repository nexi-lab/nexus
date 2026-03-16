/**
 * Zustand store for upload session management.
 *
 * The backend uses the tus.io v1.0.0 protocol which only provides:
 * - OPTIONS /api/v2/uploads         — Server capabilities
 * - POST    /api/v2/uploads         — Create upload session
 * - PATCH   /api/v2/uploads/{id}    — Upload chunk
 * - HEAD    /api/v2/uploads/{id}    — Get upload offset (for resumption)
 * - DELETE  /api/v2/uploads/{id}    — Terminate upload
 *
 * There is no GET/list endpoint in tus. Sessions are tracked client-side
 * by recording IDs from POST responses. The TUI surface is for monitoring
 * and managing sessions the user has created elsewhere.
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
}

// =============================================================================
// Store
// =============================================================================

export interface UploadState {
  /** Locally tracked sessions (added via addSession or manual entry). */
  readonly sessions: readonly UploadSession[];
  readonly selectedSessionIndex: number;
  readonly error: string | null;

  /**
   * Add a session to track. Since tus has no list endpoint, sessions
   * must be added manually (e.g., from a POST /api/v2/uploads response
   * or user input).
   */
  readonly addSession: (session: UploadSession) => void;
  /** Remove a session from tracking (does NOT call DELETE). */
  readonly removeSession: (sessionId: string) => void;
  /** Terminate an upload session via DELETE and remove from tracking. */
  readonly terminateSession: (sessionId: string, client: FetchClient) => Promise<void>;
  /**
   * Refresh a session's offset via HEAD (tus resumption check).
   * Parses Upload-Offset and Upload-Length from response headers.
   */
  readonly refreshSessionStatus: (sessionId: string, client: FetchClient) => Promise<void>;
  readonly setSelectedSessionIndex: (index: number) => void;
}

export const useUploadStore = create<UploadState>((set, get) => ({
  sessions: [],
  selectedSessionIndex: 0,
  error: null,

  addSession: (session) => {
    set((state) => {
      if (state.sessions.some((s) => s.id === session.id)) return state;
      return { sessions: [...state.sessions, session] };
    });
  },

  removeSession: (sessionId) => {
    set((state) => ({
      sessions: state.sessions.filter((s) => s.id !== sessionId),
      selectedSessionIndex: Math.min(
        state.selectedSessionIndex,
        Math.max(state.sessions.length - 2, 0),
      ),
    }));
  },

  terminateSession: async (sessionId, client) => {
    set({ error: null });
    try {
      await client.delete(`/api/v2/uploads/${encodeURIComponent(sessionId)}`);
      get().removeSession(sessionId);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to terminate upload session",
      });
    }
  },

  refreshSessionStatus: async (sessionId, client) => {
    set({ error: null });
    try {
      // HEAD returns Upload-Offset and Upload-Length headers
      const response = await client.rawRequest(
        "HEAD",
        `/api/v2/uploads/${encodeURIComponent(sessionId)}`,
      );

      if (!response.ok) {
        throw new Error(`HEAD returned ${response.status}`);
      }

      const offset = parseInt(response.headers.get("Upload-Offset") ?? "0", 10);
      const length = parseInt(response.headers.get("Upload-Length") ?? "0", 10);
      const expiresAt = response.headers.get("Upload-Expires") ?? null;

      set((state) => ({
        sessions: state.sessions.map((s) =>
          s.id === sessionId
            ? { ...s, offset, length, expires_at: expiresAt }
            : s,
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
