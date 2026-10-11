import { afterAll, describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { FetchClient, SearchClient } from "@nexus-ai-fs/api-client";
import { createWorkerManager } from "../../src/worker/worker-manager.js";
import { useSearchStore } from "../../src/stores/search-store.js";
import { SearchResults } from "../../src/panels/search/search-results.js";
import { searchFiles } from "../../src/panels/files/file-search.js";
import { testRender } from "../helpers/render.js";

const fixturePath = process.env.NEXUS_TS_SEARCH_FIXTURE;
const hybridFixturePath = process.env.NEXUS_TS_SEARCH_HYBRID_FIXTURE;

describe.skipIf(!fixturePath)("native Search through the TUI worker and terminal renderer", () => {
  const fixture = fixturePath ? JSON.parse(readFileSync(fixturePath, "utf8")) : null;
  // A wrong primary URL proves Search reaches its configured owning host.
  const manager = fixture ? createWorkerManager({ apiKey: fixture.readerKey, baseUrl: "http://127.0.0.1:1", maxRetries: 0 }) : null;
  const client = manager?.client as unknown as FetchClient;
  const connection = fixture ? { url: fixture.url, zoneId: "sharedzone" } : undefined;

  afterAll(() => manager?.terminate());

  it("renders an authorized native chunk without Python response fields", async () => {
    useSearchStore.setState({ searchMode: "keyword" });
    await useSearchStore.getState().search(fixture.needle, client, connection);
    const state = useSearchStore.getState();
    expect(state.error).toBeNull();
    expect(state.searchResults.map(hit => hit.path)).toEqual([fixture.visible]);
    expect(state.searchResults[0]!.chunk_text).toContain(fixture.needle);
    const setup = await testRender(() => <SearchResults results={state.searchResults} total={state.searchTotal} selectedIndex={0} loading={false} />, { width: 160, height: 24 });
    try {
      await setup.renderOnce();
      const frame = setup.captureCharFrame();
      expect(frame).toContain("sharedzone");
      expect(frame).toContain(fixture.needle);
      expect(frame).toContain("Results: 1 found");
    } finally {
      setup.renderer.destroy();
    }
  });

  it("discovery preserves directory access checks and returns readable current bytes", async () => {
    const indexed = await searchFiles(fixture.needle, fixture.root, client, connection);
    expect(indexed.error).toBeDefined();
    expect(indexed.hits).toEqual([]);
    await expect(searchFiles("g:**/*.txt", fixture.root, client, connection)).rejects.toThrow("search path is not readable");
    const reader = new SearchClient(client, connection);
    const readerGrep = await reader.grep(fixture.needle, fixture.root, { files: [fixture.visible, fixture.hidden] });
    expect(readerGrep.matches.map(hit => ({ path: hit.path, line: hit.line_number, content: hit.line }))).toEqual([{ path: fixture.visible, line: 2, content: `${fixture.needle} native search` }]);
    const empty = await reader.grep(fixture.needle, fixture.root, { files: [] });
    expect(empty.matches).toEqual([]);
    const adminManager = createWorkerManager({ apiKey: fixture.adminKey, baseUrl: "http://127.0.0.1:1", maxRetries: 0 });
    try {
      const adminClient = adminManager.client as unknown as FetchClient;
      const glob = await searchFiles("g:**/*.txt", fixture.root, adminClient, connection);
      expect(glob.error).toBeUndefined();
      expect(new Set(glob.hits.map(hit => hit.path))).toEqual(new Set([fixture.visible, fixture.hidden]));
      const grep = await searchFiles(`r:${fixture.needle}`, fixture.root, adminClient, connection);
      expect(grep.error).toBeUndefined();
      expect(new Set(grep.hits.map(hit => hit.path))).toEqual(new Set([fixture.visible, fixture.hidden]));
      for (const hit of grep.hits) {
        expect(hit.line).toBe(2);
        expect(hit.content).toBe(`${fixture.needle} native search`);
      }
      expect(grep.truncated).toBe(false);
    } finally {
      adminManager.terminate();
    }
  });

  it("reports truncation and does not return a revoked cached hit", async () => {
    const admin = new FetchClient({ baseUrl: fixture.url, apiKey: fixture.adminKey, maxRetries: 0 });
    const limited = await new SearchClient(admin, { zoneId: "sharedzone" }).glob("**/*.txt", fixture.root, { maxResults: 1 });
    expect(limited.paths).toHaveLength(1);
    expect(limited.truncated).toBe(true);
    const revoked = await admin.rawRequest("DELETE", "/v2/rebac/tuples", JSON.stringify(fixture.grant), { headers: { "Content-Type": "application/json" } });
    expect(revoked.status).toBe(200);
    await useSearchStore.getState().search(fixture.needle, client, connection);
    expect(useSearchStore.getState().error).toBeNull();
    expect(useSearchStore.getState().searchResults).toEqual([]);
    await admin.post("/v2/rebac/tuples", fixture.grant);
    await useSearchStore.getState().search(fixture.needle, client, connection);
    expect(useSearchStore.getState().searchResults.map(hit => hit.path)).toEqual([fixture.visible]);
    await admin.delete(`/v2/auth/keys/${fixture.keyHash}`);
    await useSearchStore.getState().search(fixture.needle, client, connection);
    const state = useSearchStore.getState();
    expect(state.searchResults).toEqual([]);
    expect(state.error).not.toBeNull();
    const setup = await testRender(() => <SearchResults results={state.searchResults} total={0} selectedIndex={0} loading={false} error={state.error} />, { width: 120, height: 24 });
    try {
      await setup.renderOnce();
      expect(setup.captureCharFrame()).toContain("Search failed");
      expect(setup.captureCharFrame()).not.toContain("No results");
    } finally {
      setup.renderer.destroy();
    }
  });

  it.skipIf(!hybridFixturePath)("file explorer hybrid and semantic queries use the real local model", async () => {
    const full = JSON.parse(readFileSync(hybridFixturePath!, "utf8"));
    const fullManager = createWorkerManager({ apiKey: full.readerKey, baseUrl: "http://127.0.0.1:1", maxRetries: 0 });
    const fullClient = fullManager.client as unknown as FetchClient;
    const fullConnection = { url: full.url, zoneId: "sharedzone" };
    try {
      const indexed = await searchFiles(full.needle, full.root, fullClient, fullConnection);
      expect(indexed.error).toBeUndefined();
      expect(indexed.hits.map(hit => hit.path)).toEqual([full.visible]);
      useSearchStore.setState({ searchMode: "semantic" });
      await useSearchStore.getState().search(full.needle, fullClient, fullConnection);
      expect(useSearchStore.getState().error).toBeNull();
      expect(useSearchStore.getState().searchResults.map(hit => hit.path)).toEqual([full.visible]);
    } finally {
      fullManager.terminate();
    }
  }, 30000);
});
