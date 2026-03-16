/**
 * Shared helper to reduce store action boilerplate (Decision 6A).
 *
 * Wraps the common try/catch/loading pattern used by all store fetch actions.
 */

import type { FetchClient } from "@nexus/api-client";

type SetState<S> = (partial: Partial<S> | ((state: S) => Partial<S>)) => void;

/**
 * Create a store action that handles loading/error state automatically.
 *
 * Usage in a Zustand store:
 * ```ts
 * fetchItems: createApiAction<MyState>(set, {
 *   loadingKey: "itemsLoading",
 *   action: async (client) => {
 *     const data = await client.get<Item[]>("/api/v2/items");
 *     return { items: data };
 *   },
 * }),
 * ```
 */
export function createApiAction<S extends { error: string | null }>(
  set: SetState<S>,
  config: {
    /** State key to set to true/false during loading. */
    readonly loadingKey: keyof S & string;
    /** Async function that calls the API and returns partial state to merge. */
    readonly action: (client: FetchClient, ...args: unknown[]) => Promise<Partial<S>>;
    /** Error message prefix if the action fails. */
    readonly errorMessage?: string;
  },
): (client: FetchClient, ...args: unknown[]) => Promise<void> {
  return async (client: FetchClient, ...args: unknown[]) => {
    set({ [config.loadingKey]: true, error: null } as Partial<S>);
    try {
      const result = await config.action(client, ...args);
      set({ ...result, [config.loadingKey]: false } as Partial<S>);
    } catch (err) {
      set({
        [config.loadingKey]: false,
        error: err instanceof Error
          ? err.message
          : (config.errorMessage ?? "Operation failed"),
      } as Partial<S>);
    }
  };
}
