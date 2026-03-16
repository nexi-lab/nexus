/**
 * Detects if the connected server is "fresh" (no user data yet).
 * Used to trigger the welcome screen on first run.
 */
import { useState, useEffect } from "react";
import { useGlobalStore } from "../../stores/global-store.js";

export function useFreshServer(): { isFresh: boolean | null; loading: boolean } {
  const client = useGlobalStore((s) => s.client);
  const connectionStatus = useGlobalStore((s) => s.connectionStatus);
  const [isFresh, setIsFresh] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
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
        const [files, agents] = await Promise.all([
          client.get<{ entries: unknown[] }>("/api/v2/files?path=/&limit=5"),
          client.get<{ agents: unknown[] }>("/api/v2/agents?limit=1&offset=0"),
        ]);
        if (!cancelled) {
          // Fresh if no files and no agents
          const hasFiles = (files.entries?.length ?? 0) > 0;
          const hasAgents = (agents.agents?.length ?? 0) > 0;
          setIsFresh(!hasFiles && !hasAgents);
        }
      } catch {
        if (!cancelled) setIsFresh(false); // Assume not fresh on error
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [client, connectionStatus]);

  return { isFresh, loading };
}
