/**
 * Shared helper to reduce store action boilerplate (Decision 6A).
 *
 * Wraps the common try/catch/loading pattern used by all store fetch actions.
 * Enhanced with onSuccess callback, error categorization, and centralized
 * error store integration (Decision 8A).
 */

import type { FetchClient } from "@nexus-ai-fs/api-client";
import { useErrorStore, type ErrorCategory } from "./error-store.js";
import { useUiStore } from "./ui-store.js";
import type { PanelId } from "./global-store.js";

type SetState<S> = (partial: Partial<S> | ((state: S) => Partial<S>)) => void;

// =============================================================================
// Error categorization
// =============================================================================

const NETWORK_PATTERNS = [
  /ECONNREFUSED/i,
  /ECONNRESET/i,
  /ETIMEDOUT/i,
  /fetch failed/i,
  /network/i,
  /timeout/i,
  /DNS/i,
] as const;

const VALIDATION_PATTERNS = [
  /invalid/i,
  /validation/i,
  /bad request/i,
  /422/i,
  /400/i,
  /missing required/i,
  /must be/i,
] as const;

/**
 * Categorize an error message into network, validation, or server.
 */
export function categorizeError(message: string): ErrorCategory {
  for (const pattern of NETWORK_PATTERNS) {
    if (pattern.test(message)) return "network";
  }
  for (const pattern of VALIDATION_PATTERNS) {
    if (pattern.test(message)) return "validation";
  }
  return "server";
}

// =============================================================================
// Action factory (signature-agnostic)
// =============================================================================

/**
 * Create a store action that handles loading/error state automatically.
 *
 * The action function can take any arguments — the wrapper preserves
 * the original function signature.
 *
 * Usage in a Zustand store:
 * ```ts
 * fetchItems: createApiAction<MyState, [client: FetchClient]>(set, {
 *   loadingKey: "itemsLoading",
 *   action: async (client) => {
 *     const data = await client.get<Item[]>("/api/v2/items");
 *     return { items: data };
 *   },
 *   source: "files",
 * }),
 *
 * // Works with any argument signature:
 * fetchStatus: createApiAction<MyState, [agentId: string, client: FetchClient]>(set, {
 *   loadingKey: "statusLoading",
 *   action: async (agentId, client) => {
 *     const data = await client.get(`/api/v2/agents/${agentId}/status`);
 *     return { status: data };
 *   },
 * }),
 * ```
 */
export function createApiAction<
  S extends { error: string | null },
  Args extends unknown[] = [FetchClient, ...unknown[]],
>(
  set: SetState<S>,
  config: {
    /** State key to set to true/false during loading. */
    readonly loadingKey: keyof S & string;
    /** Async function that calls the API and returns partial state to merge. */
    readonly action: (...args: Args) => Promise<Partial<S>>;
    /** Fallback error message used when the thrown value is not an Error instance. */
    readonly errorMessage?: string;
    /** Called after successful action (after state is merged). */
    readonly onSuccess?: () => void;
    /** Source panel ID for error store categorization. */
    readonly source?: string;
    /** Whether to push errors to the centralized error store. Default: true. */
    readonly pushToErrorStore?: boolean;
    /** Whether the action can be retried on failure. Default: false. */
    readonly retryable?: boolean;
  },
): (...args: Args) => Promise<void> {
  const pushToErrorStore = config.pushToErrorStore ?? true;

  return async (...args: Args) => {
    set({ [config.loadingKey]: true, error: null } as Partial<S>);
    try {
      const result = await config.action(...args);
      set({ ...result, [config.loadingKey]: false } as Partial<S>);
      if (config.source) {
        useUiStore.getState().markDataUpdated(config.source as PanelId);
      }
      config.onSuccess?.();
    } catch (err) {
      const message = err instanceof Error
        ? err.message
        : (config.errorMessage ?? "Operation failed");

      set({
        [config.loadingKey]: false,
        error: message,
      } as Partial<S>);

      if (pushToErrorStore) {
        const category = categorizeError(message);
        useErrorStore.getState().pushError({
          message,
          category,
          source: config.source,
          retryAction: config.retryable
            ? () => { createApiAction(set, config)(...args); }
            : undefined,
        });
      }
    }
  };
}
