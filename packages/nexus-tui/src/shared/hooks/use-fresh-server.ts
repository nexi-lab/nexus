/**
 * Detects if the connected server is "fresh" (no user data yet).
 * Used to trigger the welcome screen on first run.
 */
import { createEffect, createSignal, onCleanup } from "solid-js";
import { useGlobalStore } from "../../stores/global-store.js";

interface FreshCheckClient {
  get<T>(url: string): Promise<T>;
}

/**
 * Pure detection logic — exported for testing without React hooks.
 * Returns true if the server has no files and no agents.
 */
export async function detectFreshServer(client: FreshCheckClient): Promise<boolean> {
  try {
    const [files, agents] = await Promise.all([
      client.get<{ entries: unknown[] }>("/api/v2/files?path=/&limit=5"),
      client.get<{ agents: unknown[] }>("/api/v2/agents?limit=1&offset=0"),
    ]);
    const hasFiles = (files.entries?.length ?? 0) > 0;
    const hasAgents = (agents.agents?.length ?? 0) > 0;
    return !hasFiles && !hasAgents;
  } catch {
    return false; // Assume not fresh on error
  }
}

export function useFreshServer(): { isFresh: boolean | null; loading: boolean } {
  const client = useGlobalStore((s) => s.client);
  const connectionStatus = useGlobalStore((s) => s.connectionStatus);
  const [isFresh, setIsFresh] = createSignal<boolean | null>(null);
  const [loading, setLoading] = createSignal(false);

  createEffect(() => {
    if (connectionStatus !== "connected" || !client) {
      setIsFresh(null);
      return;
    }

    let cancelled = false;
    setLoading(true);

    // Check if server has any user-created data
    // A "fresh" server has no files beyond defaults and no agents
    (async () => {
      try {
        const result = await detectFreshServer(client);
        if (!cancelled) setIsFresh(result);
      } catch {
        if (!cancelled) setIsFresh(false);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    onCleanup(() => { cancelled = true; });
  });

  return {
    get isFresh() {
      return isFresh();
    },
    get loading() {
      return loading();
    },
  };
}
