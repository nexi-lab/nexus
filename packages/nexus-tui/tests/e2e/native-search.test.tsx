import { afterAll, describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { FetchClient, IndexingClient, SearchClient, VfsClient, type IndexingMode } from "@nexus-ai-fs/api-client";
import { createWorkerManager } from "../../src/worker/worker-manager.js";
import { useSearchStore } from "../../src/stores/search-store.js";
import { useGlobalStore } from "../../src/stores/global-store.js";
import { StatusBar } from "../../src/shared/components/status-bar.js";
import { SearchResults } from "../../src/panels/search/search-results.js";
import { searchFiles } from "../../src/panels/files/file-search.js";
import { testRender, waitForFrame } from "../helpers/render.js";
import { App } from "../../src/app.js";

const fixturePath = process.env.NEXUS_TS_SEARCH_FIXTURE;
const hybridFixturePath = process.env.NEXUS_TS_SEARCH_HYBRID_FIXTURE;

describe.skipIf(!fixturePath)("native Search through the TUI worker and terminal renderer", () => {
  const fixture = fixturePath ? JSON.parse(readFileSync(fixturePath, "utf8")) : null;
  // A wrong primary URL proves Search reaches its configured owning host.
  const manager = fixture ? createWorkerManager({ apiKey: fixture.readerKey, baseUrl: "http://127.0.0.1:1", maxRetries: 0 }) : null;
  const client = manager?.client as unknown as FetchClient;
  const connection = fixture ? { url: fixture.url, zoneId: "sharedzone" } : undefined;

  afterAll(() => manager?.terminate());

  it("runs the full terminal file editor against actual native VFS", async () => {
    const config = { apiKey: fixture.adminKey, baseUrl: fixture.url, zoneId: "sharedzone", maxRetries: 0, transformKeys: false };
    const administrator = createWorkerManager(config);
    const files = new VfsClient(administrator.client as unknown as FetchClient);
    const directory = `${fixture.root}/native-ui`;
    const path = `${directory}/notes.txt`;
    let setup: Awaited<ReturnType<typeof testRender>> | undefined;
    try {
      await files.mkdir(directory);
      await files.write(path, "start");
      useGlobalStore.setState({ config, client: administrator.client as unknown as FetchClient, activePanel: "files" });
      await useGlobalStore.getState().testConnection();
      expect(useGlobalStore.getState().connectionStatus).toBe("connected");
      setup = await testRender(() => <App />, { width: 180, height: 35 });
      await waitForFrame(setup, frame => frame.includes("Files: /") && !frame.includes("Loading…") && !frame.includes("Error: "));
      setup.mockInput.pressKey("g");
      await waitForFrame(setup, "Directory path (Enter");
      setup.mockInput.pressKey("END");
      await setup.mockInput.pasteBracketedText(directory.slice(1));
      setup.mockInput.pressEnter();
      await waitForFrame(setup, `FILE ${path} (`);
      setup.mockInput.pressKey("e");
      await waitForFrame(setup, "Ctrl+S:save");
      setup.mockInput.pressKey("END");
      await setup.mockInput.typeText(" updated");
      await waitForFrame(setup, "[modified]");
      setup.mockInput.pressKey("s", { ctrl: true });
      await waitForFrame(setup, `${path} | Ctrl+S:save`);
      expect(new TextDecoder().decode(await files.read(path))).toBe("start updated");
    } finally {
      setup?.renderer.destroy();
      useGlobalStore.getState().setConnectionStatus("disconnected");
      useGlobalStore.setState({ client: null });
      try { await files.delete(directory, true); } finally { administrator.terminate(); }
    }
  }, 10000);

  it("preserves actual native file bytes and mutation receipts through the worker", async () => {
    const administrator = createWorkerManager({ apiKey: fixture.adminKey, baseUrl: fixture.url, maxRetries: 0 });
    const files = new VfsClient(administrator.client as unknown as FetchClient);
    const directory = `${fixture.root}/native-files`;
    const path = `${directory}/日本語 & bytes.bin`;
    const renamed = `${directory}/renamed.bin`;
    try {
      await files.mkdir(directory);
      const bytes = new Uint8Array([0, 255, 254, 128, 13, 10]);
      const receipt = await files.write(path, bytes);
      expect(receipt.size).toBe(bytes.length);
      expect(receipt.content_id).toBeTruthy();
      expect(await files.read(path)).toEqual(bytes);
      expect((await files.metadata(path)).is_directory).toBe(false);
      expect((await files.list(directory)).map((entry) => entry.path)).toContain(path);
      await files.rename(path, renamed);
      expect(await files.read(renamed)).toEqual(bytes);
      expect(await files.delete(renamed)).toBe(true);
      expect(await files.list(directory)).toEqual([]);
    } finally {
      try { await files.delete(directory, true); } finally { administrator.terminate(); }
    }
  });

  it("connects to the native daemon and renders its authenticated identity", async () => {
    const config = {
      apiKey: fixture.readerKey, baseUrl: fixture.url, maxRetries: 0,
      agentId: "forged-operator", subject: "user:forged-operator", transformKeys: false,
    };
    const identityManager = createWorkerManager(config);
    const expected = fixture.grant.subject_id;
    try {
      useGlobalStore.setState({ config, client: identityManager.client as unknown as FetchClient, userInfo: null });
      const resolved = new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(() => { unsubscribe(); reject(new Error("Native identity did not resolve")); }, 8000);
        const unsubscribe = useGlobalStore.subscribe((state) => {
          if (state.userInfo) { clearTimeout(timeout); unsubscribe(); resolve(); }
        });
      });
      await useGlobalStore.getState().testConnection();
      await resolved;
      const state = useGlobalStore.getState();
      expect(state.connectionStatus).toBe("connected");
      expect(state.serverVersion).toBeTruthy();
      expect(state.userInfo).toMatchObject({ subject_type: "user", subject_id: expected, user_id: expected, is_admin: false });
      const setup = await testRender(() => <StatusBar />, { width: 240, height: 2 });
      try {
        await setup.renderOnce();
        expect(setup.captureCharFrame()).toContain(`user:${expected}`);
        expect(setup.captureCharFrame()).not.toContain("forged-operator");
      } finally {
        setup.renderer.destroy();
      }
    } finally {
      useGlobalStore.getState().setConnectionStatus("disconnected");
      useGlobalStore.setState({ client: null });
      identityManager.terminate();
    }
  });

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
    // Await worker replies before asserting: https://github.com/oven-sh/bun/issues/37189.
    let denied: unknown;
    try {
      await searchFiles("g:**/*.txt", fixture.root, client, connection);
    } catch (error) {
      denied = error;
    }
    expect(denied).toBeInstanceOf(Error);
    expect((denied as Error).message).toContain("search path is not readable");
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

  it("preserves management scope and directory facts through the real worker", async () => {
    const adminManager = createWorkerManager({ apiKey: fixture.adminKey, baseUrl: "http://127.0.0.1:1", maxRetries: 0 });
    const adminClient = adminManager.client as unknown as FetchClient;
    const indexing = new IndexingClient(adminClient, connection);
    const directory = `${fixture.root}/configuration & facts`;
    let previousMode: IndexingMode = "on";
    try {
      const existingModes = await indexing.listIndexingModes();
      expect(existingModes.error).toBeUndefined();
      previousMode = existingModes.modes.find(mode => mode.zone_id === "sharedzone")?.mode ?? "on";
      const health = await new SearchClient(adminClient, connection).health();
      expect(["healthy", "degraded"]).toContain(health.status);
      expect(health.fts_writer_faults).toBe(0);
      const located = await new SearchClient(client, connection).locate(fixture.visible);
      expect(located.indexed).toBe(true);
      expect(located.zone_id).toBe("sharedzone");
      expect(located.chunk_count).toBeGreaterThan(0);
      expect(await indexing.addDirectory(directory)).toEqual({ added: true });
      expect(await indexing.addDirectory(directory)).toEqual({ added: false });
      const listed = await indexing.listDirectories();
      expect(listed.error).toBeUndefined();
      const fact = listed.directories.find(entry => entry.path === directory);
      expect(fact?.zone_id).toBe("sharedzone");
      expect(fact?.added_at_ms).toBeGreaterThan(0);
      expect((await indexing.listDirectories()).directories.find(entry => entry.path === directory)).toEqual(fact);
      expect(await indexing.setIndexingMode("sandbox")).toEqual({});
      expect((await indexing.listIndexingModes()).modes).toContainEqual({ zone_id: "sharedzone", mode: "sandbox" });
      const stats = await indexing.stats();
      expect(stats.error).toBeUndefined();
      expect(stats.fts_path_count).toBeGreaterThanOrEqual(2);
      expect(stats.last_index_seq).toBeGreaterThan(0);
      const reader = new IndexingClient(new FetchClient({ baseUrl: "http://127.0.0.1:1", apiKey: fixture.readerKey, maxRetries: 0 }), connection);
      for (const operation of [() => reader.stats(), () => reader.listDirectories(), () => reader.addDirectory(directory), () => reader.removeDirectory(directory), () => reader.setIndexingMode("off"), () => reader.listIndexingModes()]) {
        let denied: unknown;
        try { await operation(); } catch (error) { denied = error; }
        expect(denied).toMatchObject({ status: 403 });
      }
      expect(await indexing.removeDirectory(directory)).toEqual({ removed: true });
      expect(await indexing.removeDirectory(directory)).toEqual({ removed: false });
      expect((await indexing.listDirectories()).directories.some(entry => entry.path === directory)).toBe(false);
    } finally {
      try {
        await indexing.removeDirectory(directory);
        await indexing.setIndexingMode(previousMode);
      } finally {
        adminManager.terminate();
      }
    }
  });

  it("indexes current VFS content and preserves skip and eviction receipts through the worker", async () => {
    const adminManager = createWorkerManager({ apiKey: fixture.adminKey, baseUrl: "http://127.0.0.1:1", maxRetries: 0 });
    const adminClient = adminManager.client as unknown as FetchClient;
    const indexing = new IndexingClient(adminClient, connection);
    const search = new SearchClient(client, connection);
    try {
      const before = (await indexing.stats()).last_index_seq;
      const deleted = await indexing.deleteFile(fixture.visible);
      expect(deleted.status).toBe("deleted");
      expect(deleted.zone_id).toBe("sharedzone");
      expect(deleted.index_seq).toBeGreaterThan(before);
      expect((await search.locate(fixture.visible)).indexed).toBe(false);
      expect((await search.query(fixture.needle, "keyword")).results).toEqual([]);
      const restored = await indexing.indexFile(fixture.visible);
      expect(restored.status).toBe("indexed");
      expect(restored.index_seq).toBeGreaterThan(deleted.index_seq);
      expect((await search.query(fixture.needle, "keyword")).results.map(hit => hit.path)).toEqual([fixture.visible]);
      const binary = await indexing.indexFile(fixture.binary);
      expect(binary).toEqual({ status: "skipped", reason: "non_text", index_seq: 0, zone_id: "sharedzone" });
      const empty = await indexing.indexFile(fixture.empty);
      expect(empty.status).toBe("skipped");
      expect(empty.reason).toBe("empty");
      expect(empty.index_seq).toBeGreaterThan(restored.index_seq);
      expect((await indexing.stats()).last_index_seq).toBeGreaterThanOrEqual(empty.index_seq);
      const reader = new IndexingClient(client, connection);
      for (const operation of [() => reader.indexFile(fixture.visible), () => reader.deleteFile(fixture.visible), () => reader.indexDocuments([{ path: fixture.visible, text: "unauthorized" }])]) {
        let denied: unknown;
        try { await operation(); } catch (error) { denied = error; }
        expect(denied).toMatchObject({ status: 403 });
      }
    } finally {
      try { await indexing.indexFile(fixture.visible); } finally { adminManager.terminate(); }
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
