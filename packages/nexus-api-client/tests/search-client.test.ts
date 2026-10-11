import { createServer, type Server } from "node:http";
import { once } from "node:events";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";
import { FetchClient } from "../src/fetch-client.js";
import { SearchClient } from "../src/search-client.js";

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
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    requests.push({ path: req.url!, authorization: req.headers.authorization, body: JSON.parse(Buffer.concat(chunks).toString() || "null") });
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(reply));
  });
  servers.push(server);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, requests };
}

describe("Rust HTTP Search client", () => {
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
