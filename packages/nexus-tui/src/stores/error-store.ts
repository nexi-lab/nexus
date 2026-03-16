/**
 * Centralized structured error store.
 *
 * Replaces fragmented per-store `error: string | null` fields with
 * categorized, dismissable errors that support retry and source filtering.
 *
 * @see Issue #3066 Architecture Decision 8A
 */

import { create } from "zustand";

// =============================================================================
// Types
// =============================================================================

export type ErrorCategory = "network" | "validation" | "server";

export interface AppError {
  readonly id: string;
  readonly message: string;
  readonly category: ErrorCategory;
  readonly source?: string;
  readonly dismissable: boolean;
  readonly retryAction?: () => void;
  readonly timestamp: number;
}

export interface PushErrorOptions {
  readonly message: string;
  readonly category: ErrorCategory;
  readonly source?: string;
  readonly dismissable?: boolean;
  readonly retryAction?: () => void;
}

export interface ErrorState {
  readonly errors: readonly AppError[];

  // Actions
  readonly pushError: (options: PushErrorOptions) => void;
  readonly dismissError: (id: string) => void;
  readonly dismissAll: () => void;
  readonly getErrorsForSource: (source: string) => readonly AppError[];
  readonly hasErrors: () => boolean;
}

// =============================================================================
// Constants
// =============================================================================

const MAX_ERRORS = 50;

let errorCounter = 0;

// =============================================================================
// Store
// =============================================================================

export const useErrorStore = create<ErrorState>((set, get) => ({
  errors: [],

  pushError: (options) => {
    const error: AppError = {
      id: `err-${++errorCounter}-${Date.now()}`,
      message: options.message,
      category: options.category,
      source: options.source,
      dismissable: options.dismissable ?? true,
      retryAction: options.retryAction,
      timestamp: Date.now(),
    };

    set((state) => {
      const next = [...state.errors, error];
      // Evict oldest if over limit
      if (next.length > MAX_ERRORS) {
        return { errors: next.slice(next.length - MAX_ERRORS) };
      }
      return { errors: next };
    });
  },

  dismissError: (id) => {
    set((state) => ({
      errors: state.errors.filter((e) => e.id !== id),
    }));
  },

  dismissAll: () => {
    set((state) => ({
      errors: state.errors.filter((e) => !e.dismissable),
    }));
  },

  getErrorsForSource: (source) => {
    return get().errors.filter((e) => e.source === source);
  },

  hasErrors: () => {
    return get().errors.length > 0;
  },
}));
