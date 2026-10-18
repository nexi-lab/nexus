import { expect, it } from "bun:test";
import { createWorkerManager } from "../../src/worker/worker-manager.js";
import { ConflictError, FetchClient, ForbiddenError, IndexingClient, NexusApiError, RateLimitError } from "@nexus-ai-fs/api-client";

it("keeps pending reads separate after a worker credential change", async () => {
  let firstSeen!: () => void;
  let releaseFirst!: () => void;
  const received = new Promise<void>(resolve => { firstSeen = resolve; });
  const held = new Promise<void>(resolve => { releaseFirst = resolve; });
  const credentials: (string | null)[] = [];
  const server = Bun.serve({
    hostname: "127.0.0.1",
    port: 0,
    async fetch(request) {
      const credential = request.headers.get("authorization");
      credentials.push(credential);
      if (credentials.length === 1) {
        firstSeen();
        await held;
      }
      return Response.json({ credential });
    },
  });
  const config = { baseUrl: `http://127.0.0.1:${server.port}`, maxRetries: 0 };
  const manager = createWorkerManager({ ...config, apiKey: "first-caller" });
  const options = { timeout: 5000 };
  const first = manager.client.get<{ credential: string }>("/probe", options);
  try {
    await Promise.race([received, first]);
    manager.reconfigure({ ...config, apiKey: "second-caller" });
    const second = await manager.client.get<{ credential: string }>("/probe", options);
    expect(second.credential).toBe("Bearer second-caller");
    expect(credentials).toEqual(["Bearer first-caller", "Bearer second-caller"]);
    releaseFirst();
    expect((await first).credential).toBe("Bearer first-caller");
  } finally {
    releaseFirst();
    await first.catch(() => {});
    manager.terminate();
    server.stop(true);
  }
}, 15000);

it("preserves mutation receipts, skips and HTTP failures through direct and worker clients", async () => {
  const calls: { method: string; path: string | null; zone: string | null; credential: string | null; body: string }[] = [];
  const server = Bun.serve({
    hostname: "127.0.0.1", port: 0,
    async fetch(request) {
      const url = new URL(request.url);
      const body = await request.text();
      const input = body ? JSON.parse(body) : {};
      const path = input.path ?? url.searchParams.get("path");
      calls.push({ method: request.method, path,
        zone: input.zone_id ?? url.searchParams.get("zone_id"),
        credential: request.headers.get("authorization"), body });
      if (path === "/empty") {
        return Response.json({ status: "skipped", reason: "empty", index_seq: 23, zone_id: "sharedzone" }, { status: 409 });
      }
      if (path === "/busy") {
        return Response.json({ detail: "indexing capacity exhausted" }, { status: 429, headers: { "Retry-After": "5" } });
      }
      if (path === "/denied") {
        return Response.json({ detail: "management requires admin" }, { status: 403 });
      }
      if (path === "/malformed") {
        return Response.json({ status: "skipped", reason: "empty" }, { status: 409 });
      }
      if (url.pathname.endsWith("/batch")) {
        if (input.documents[0].path === "/failed") {
          return Response.json({ error: "storage unavailable" }, { status: 500 });
        }
        return Response.json({ indexed_count: 1, skipped_count: 1, skipped_paths: ["/blank"], index_seq: 24 });
      }
      return Response.json({ status: request.method === "DELETE" ? "deleted" : "indexed", index_seq: 25, zone_id: "sharedzone" });
    },
  });
  const config = { baseUrl: "http://127.0.0.1:1", apiKey: "original-caller", maxRetries: 0 };
  const manager = createWorkerManager(config);
  try {
    for (const transport of [new FetchClient(config), manager.client as unknown as FetchClient]) {
      const indexing = new IndexingClient(transport, { url: `http://127.0.0.1:${server.port}`, zoneId: "sharedzone" });
      expect(await indexing.indexFile("/empty")).toEqual({ status: "skipped", reason: "empty", index_seq: 23, zone_id: "sharedzone" });
      for (const [path, status, code] of [["/busy", 429, "rate_limit_error"], ["/denied", 403, "forbidden"], ["/malformed", 409, "conflict"]] as const) {
        let failure: unknown;
        try { await indexing.indexFile(path); } catch (error) { failure = error; }
        expect(failure).toBeInstanceOf(NexusApiError);
        expect(failure).toMatchObject({ status, code });
        if (status === 403) expect(failure).toBeInstanceOf(ForbiddenError);
        if (status === 409) expect(failure).toBeInstanceOf(ConflictError);
        if (status === 429) {
          expect(failure).toBeInstanceOf(RateLimitError);
          expect(failure).toMatchObject({ retryAfter: 5, body: { detail: "indexing capacity exhausted" } });
        }
      }
      let failure: unknown;
      try { await indexing.indexDocuments([{ path: "/failed", text: "gentian" }]); } catch (error) { failure = error; }
      expect(failure).toBeInstanceOf(NexusApiError);
      expect(failure).toMatchObject({ status: 500, body: { error: "storage unavailable" } });
      expect(await indexing.indexDocuments([{ path: "/inline", text: "gentian" }, { path: "/blank", text: "" }]))
        .toEqual({ indexed_count: 1, skipped_count: 1, skipped_paths: ["/blank"], index_seq: 24 });
      const escaped = '/space & quote" 虹.md';
      expect(await indexing.deleteFile(escaped)).toEqual({ status: "deleted", index_seq: 25, zone_id: "sharedzone" });
      expect(calls.at(-1)).toEqual({ method: "DELETE", path: escaped, zone: "sharedzone", credential: "Bearer original-caller", body: "" });
    }
    expect(calls).toHaveLength(14);
    expect(calls.every(call => call.credential === "Bearer original-caller")).toBe(true);
  } finally {
    manager.terminate();
    server.stop(true);
  }
}, 15000);
