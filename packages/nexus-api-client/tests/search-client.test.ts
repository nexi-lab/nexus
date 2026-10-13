import { createServer, type Server } from "node:http";
import { once } from "node:events";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";
import { FetchClient } from "../src/fetch-client.js";
import { SearchClient } from "../src/search-client.js";
import { IndexingClient } from "../src/indexing-client.js";

const servers: Server[] = [];

afterEach(async () => {
  for (const server of servers.splice(0)) {
    const closed = once(server, "close");
    server.close();
    server.closeAllConnections();
    await closed;
  }
});

async function serve(status: number, reply: unknown) {
  const requests: { path: string; authorization?: string; body: unknown }[] = [];
  const methods: string[] = [];
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    methods.push(req.method!);
    requests.push({ path: req.url!, authorization: req.headers.authorization, body: JSON.parse(Buffer.concat(chunks).toString() || "null") });
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(reply));
  });
  servers.push(server);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, requests, methods };
}

describe("Rust HTTP Search client", () => {
  it("reads health and per-file status from the owning host with the original credential", async () => {
    const health = { status: "degraded", detail: "keyword only", fts_writer_faults: 0, fts_writer_unavailable: 0, last_verified_commit_age_ms: null, dispatch_panics: 2 };
    const status = { indexed: true, chunk_count: 2, mtime_ms: null, zone_id: "legal" };
    const healthHost = await serve(200, health);
    const locateHost = await serve(200, status);
    const transport = new FetchClient({ baseUrl: "http://127.0.0.1:1", apiKey: "status-caller", maxRetries: 0 });
    expect(await new SearchClient(transport, { url: healthHost.url }).health()).toEqual(health);
    expect(await new SearchClient(transport, { url: locateHost.url, zoneId: "legal" }).locate("/legal/a.txt")).toEqual(status);
    expect(healthHost.requests).toEqual([{ path: "/v2/search/health", authorization: "Bearer status-caller", body: null }]);
    expect(locateHost.requests).toEqual([{ path: "/v2/search/locate", authorization: "Bearer status-caller", body: { path: "/legal/a.txt", zone_id: "legal" } }]);
  });
  it("sends one batch with the caller credential and preserves positional failures", async () => {
    const responses = [{ results: [{ path: "/docs/a", zone_id: "legal", chunk_text: "native" }] }, { results: [], error: "empty query" }];
    const host = await serve(200, { responses });
    const search = new SearchClient(new FetchClient({ baseUrl: host.url, apiKey: "batch-caller", maxRetries: 0 }), { zoneId: "sharedzone" });
    expect(await search.batchQuery([{ q: "a", zone_id: "legal" }, { q: "" }])).toEqual({ responses });
    expect(host.requests).toEqual([{
      path: "/v2/search/query/batch", authorization: "Bearer batch-caller",
      body: { queries: [{ q: "a", zone_id: "legal" }, { q: "", zone_id: "sharedzone" }] },
    }]);
  });
  it("uses the explicit host with the original credential and preserves wire attribution", async () => {
    const primary = await serve(404, {});
    const hit = { path: "/docs/a.txt", zone_id: "sharedzone", chunk_text: "native", chunk_index: 0, score: 1, keyword_score: 0.5 };
    const host = await serve(200, { results: [hit] });
    const transport = new FetchClient({ baseUrl: primary.url, apiKey: "exact.credential", maxRetries: 0 });
    const search = new SearchClient(transport, { url: `${host.url}/`, zoneId: "sharedzone" });
    expect(await search.query("a & b", "keyword")).toEqual({ results: [hit] });
    expect(primary.requests).toHaveLength(0);
    expect(host.requests).toEqual([{
      path: "/v2/search/query", authorization: "Bearer exact.credential",
      body: { q: "a & b", query_type: "keyword", path_filter: "/", limit: 10, zone_id: "sharedzone" },
    }]);
    // The per-request wire override must not change unrelated API calls.
    expect(await transport.post<{ results: { chunkText: string }[] }>("/ordinary", {}, { baseUrl: host.url })).toEqual({ results: [{ path: "/docs/a.txt", zoneId: "sharedzone", chunkText: "native", chunkIndex: 0, score: 1, keywordScore: 0.5 }] });
  });

  it("uses the shared origin when unconfigured and preserves partial-query errors", async () => {
    const host = await serve(200, { results: [], error: "remote zone unavailable" });
    const search = new SearchClient(new FetchClient({ baseUrl: host.url, apiKey: "caller", maxRetries: 0 }));
    expect(await search.query("needle")).toEqual({ results: [], error: "remote zone unavailable" });
    expect(host.requests[0]!.body).toMatchObject({ zone_id: "" });
  });

  it("preserves Rust authorization errors", async () => {
    const host = await serve(403, { error: "caller cannot read this zone" });
    const search = new SearchClient(new FetchClient({ baseUrl: host.url, apiKey: "caller", maxRetries: 0 }));
    await expect(search.grep("needle", "/docs")).rejects.toMatchObject({ status: 403, message: "caller cannot read this zone" });
  });

  it("preserves glob paths and grep line numbers and truncation", async () => {
    const glob = await serve(200, { paths: ["/docs/a.txt"], truncated: true });
    const grep = await serve(200, { matches: [{ path: "/docs/a.txt", line_number: 2, line: "needle" }], truncated: false });
    const transport = new FetchClient({ baseUrl: glob.url, apiKey: "caller", maxRetries: 0 });
    expect(await new SearchClient(transport).glob("**/*.txt", "/docs")).toEqual({ paths: ["/docs/a.txt"], truncated: true });
    expect(await new SearchClient(transport, { url: grep.url }).grep("needle", "/docs")).toEqual({ matches: [{ path: "/docs/a.txt", line_number: 2, line: "needle" }], truncated: false });
    await new SearchClient(transport).glob("**/*.txt", "/docs", { files: [] });
    expect(glob.requests[1]!.body).toMatchObject({ files: [], max_results: 100 });
  });
});

describe("Rust HTTP indexing client", () => {
  it("encodes structured query fields once and avoids duplicate scope parameters", async () => {
    const host = await serve(200, {});
    const transport = new FetchClient({ baseUrl: host.url, apiKey: "query-caller", maxRetries: 0 });
    await transport.get("/probe?zone_id=old&keep=yes", { query: { zone_id: "legal & docs", limit: 3, recursive: false, absent: undefined } });
    const url = new URL(host.requests[0]!.path, host.url);
    expect(url.searchParams.getAll("zone_id")).toEqual(["legal & docs"]);
    expect(Object.fromEntries(url.searchParams)).toEqual({ zone_id: "legal & docs", keep: "yes", limit: "3", recursive: "false" });
    expect(host.requests[0]!.authorization).toBe("Bearer query-caller");
  });
  it("keeps directory registration and removal separate and preserves host verdicts", async () => {
    const directory = { path: "/legal/project & docs", zone_id: "legal", added_at_ms: 1700000000000 };
    const listed = await serve(200, { directories: [directory] });
    const added = await serve(200, { added: false, error: "configuration unavailable" });
    const removed = await serve(200, { removed: false });
    const transport = new FetchClient({ baseUrl: "http://127.0.0.1:1", apiKey: "management-caller", maxRetries: 0 });
    const clientFor = (url: string) => new IndexingClient(transport, { url, zoneId: "legal" });
    expect(await clientFor(listed.url).listDirectories()).toEqual({ directories: [directory] });
    expect(await clientFor(added.url).addDirectory(directory.path)).toEqual({ added: false, error: "configuration unavailable" });
    expect(await clientFor(removed.url).removeDirectory(directory.path)).toEqual({ removed: false });
    expect(listed.requests[0]).toEqual({ path: "/v2/documents/directories?zone_id=legal", authorization: "Bearer management-caller", body: null });
    expect(added.requests[0]).toEqual({ path: "/v2/documents/directories", authorization: "Bearer management-caller", body: { path: directory.path, zone_id: "legal" } });
    const removeUrl = new URL(removed.requests[0]!.path, removed.url);
    expect(removeUrl.pathname).toBe("/v2/documents/directories");
    expect(Object.fromEntries(removeUrl.searchParams)).toEqual({ path: directory.path, zone_id: "legal" });
    expect(removed.requests[0]!.body).toBeNull();
    expect([listed.methods[0], added.methods[0], removed.methods[0]]).toEqual(["GET", "POST", "DELETE"]);
    expect(removed.requests[0]!.authorization).toBe("Bearer management-caller");
  });

  it("preserves mode scope, reset semantics and the host's statistics without casing transforms", async () => {
    const modes = { modes: [{ zone_id: "legal", mode: "sandbox" }] };
    const statistics = { fts_doc_count: 5, fts_path_count: 3, last_index_seq: 7, pending: 2, last_successful_index_at_ms: 1700000000000 };
    const configured = await serve(200, {});
    const listed = await serve(200, modes);
    const counted = await serve(200, statistics);
    const transport = new FetchClient({ baseUrl: "http://127.0.0.1:1", apiKey: "mode-caller", maxRetries: 0 });
    expect(await new IndexingClient(transport, { url: configured.url, zoneId: "legal" }).setIndexingMode("")).toEqual({});
    expect(await new IndexingClient(transport, { url: listed.url }).listIndexingModes()).toEqual(modes);
    expect(await new IndexingClient(transport, { url: counted.url, zoneId: "legal" }).stats()).toEqual(statistics);
    expect(configured.requests[0]).toEqual({ path: "/v2/documents/indexing-modes", authorization: "Bearer mode-caller", body: { zone_id: "legal", mode: "" } });
    expect(configured.methods).toEqual(["PUT"]);
    expect(counted.requests[0]!.path).toBe("/v2/documents/stats?zone_id=legal");
    expect(counted.requests[0]!.authorization).toBe("Bearer mode-caller");
  });
});
