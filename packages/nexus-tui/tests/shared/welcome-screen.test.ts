/**
 * Tests for fresh server detection logic.
 *
 * Tests the exported detectFreshServer function (real production code)
 * rather than a local reimplementation.
 */

import { describe, it, expect, mock } from "bun:test";
import { detectFreshServer } from "../../src/shared/hooks/use-fresh-server.js";

function mockClient(responses: Record<string, unknown>) {
  return {
    get: mock(async (url: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (url.includes(pattern)) return response;
      }
      throw new Error(`Unmocked: ${url}`);
    }),
  };
}

describe("detectFreshServer", () => {
  it("returns true when API returns empty lists", async () => {
    const client = mockClient({
      "/api/v2/files": { entries: [] },
      "/api/v2/agents": { agents: [] },
    });
    expect(await detectFreshServer(client)).toBe(true);
  });

  it("returns false when API returns files", async () => {
    const client = mockClient({
      "/api/v2/files": { entries: [{ name: "readme.md" }] },
      "/api/v2/agents": { agents: [] },
    });
    expect(await detectFreshServer(client)).toBe(false);
  });

  it("returns false when API returns agents", async () => {
    const client = mockClient({
      "/api/v2/files": { entries: [] },
      "/api/v2/agents": { agents: [{ agent_id: "bot-1" }] },
    });
    expect(await detectFreshServer(client)).toBe(false);
  });

  it("returns false when API returns both", async () => {
    const client = mockClient({
      "/api/v2/files": { entries: [{ name: "data.json" }] },
      "/api/v2/agents": { agents: [{ agent_id: "bot-1" }] },
    });
    expect(await detectFreshServer(client)).toBe(false);
  });

  it("treats missing fields as fresh", async () => {
    const client = { get: mock(async () => ({})) };
    expect(await detectFreshServer(client)).toBe(true);
  });

  it("treats API errors as not fresh", async () => {
    const client = { get: mock(async () => { throw new Error("Network error"); }) };
    expect(await detectFreshServer(client)).toBe(false);
  });
});
