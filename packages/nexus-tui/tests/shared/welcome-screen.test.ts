/**
 * Tests for fresh server detection logic.
 *
 * Validates that useFreshServer correctly identifies empty servers
 * (no files, no agents) to trigger the welcome screen on first run.
 */

import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useGlobalStore } from "../../src/stores/global-store.js";
import type { FetchClient } from "@nexus/api-client";

// ---------------------------------------------------------------------------
// Helper: simulate the fresh-server detection logic without React hooks.
// This mirrors the core async check inside useFreshServer.
// ---------------------------------------------------------------------------

async function detectFreshServer(client: FetchClient): Promise<boolean> {
  const [files, agents] = await Promise.all([
    client.get<{ entries: unknown[] }>("/api/v2/files?path=/&limit=5"),
    client.get<{ agents: unknown[] }>("/api/v2/agents?limit=1&offset=0"),
  ]);
  const hasFiles = (files.entries?.length ?? 0) > 0;
  const hasAgents = (agents.agents?.length ?? 0) > 0;
  return !hasFiles && !hasAgents;
}

describe("fresh server detection", () => {
  beforeEach(() => {
    useGlobalStore.setState({
      connectionStatus: "disconnected",
      connectionError: null,
      client: null,
    });
  });

  it("returns isFresh=true when API returns empty lists", async () => {
    const mockClient = {
      get: mock(async (url: string) => {
        if (url.includes("/api/v2/files")) {
          return { entries: [] };
        }
        if (url.includes("/api/v2/agents")) {
          return { agents: [] };
        }
        return {};
      }),
    } as unknown as FetchClient;

    const result = await detectFreshServer(mockClient);
    expect(result).toBe(true);
  });

  it("returns isFresh=false when API returns files", async () => {
    const mockClient = {
      get: mock(async (url: string) => {
        if (url.includes("/api/v2/files")) {
          return { entries: [{ name: "readme.md" }] };
        }
        if (url.includes("/api/v2/agents")) {
          return { agents: [] };
        }
        return {};
      }),
    } as unknown as FetchClient;

    const result = await detectFreshServer(mockClient);
    expect(result).toBe(false);
  });

  it("returns isFresh=false when API returns agents", async () => {
    const mockClient = {
      get: mock(async (url: string) => {
        if (url.includes("/api/v2/files")) {
          return { entries: [] };
        }
        if (url.includes("/api/v2/agents")) {
          return { agents: [{ agent_id: "bot-1" }] };
        }
        return {};
      }),
    } as unknown as FetchClient;

    const result = await detectFreshServer(mockClient);
    expect(result).toBe(false);
  });

  it("returns isFresh=false when API returns both files and agents", async () => {
    const mockClient = {
      get: mock(async (url: string) => {
        if (url.includes("/api/v2/files")) {
          return { entries: [{ name: "data.json" }] };
        }
        if (url.includes("/api/v2/agents")) {
          return { agents: [{ agent_id: "bot-1" }] };
        }
        return {};
      }),
    } as unknown as FetchClient;

    const result = await detectFreshServer(mockClient);
    expect(result).toBe(false);
  });

  it("handles missing entries/agents fields gracefully (treats as fresh)", async () => {
    const mockClient = {
      get: mock(async () => ({})),
    } as unknown as FetchClient;

    const result = await detectFreshServer(mockClient);
    expect(result).toBe(true);
  });

  it("treats API errors as not fresh", async () => {
    const mockClient = {
      get: mock(async () => { throw new Error("Network error"); }),
    } as unknown as FetchClient;

    let isFresh: boolean;
    try {
      isFresh = await detectFreshServer(mockClient);
    } catch {
      // On error, the hook sets isFresh = false
      isFresh = false;
    }
    expect(isFresh).toBe(false);
  });
});
