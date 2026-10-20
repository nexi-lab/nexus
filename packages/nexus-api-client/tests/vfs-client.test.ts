import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { createServer, type Server } from "node:http";
import { FetchClient } from "../src/fetch-client.js";
import { VfsClient } from "../src/vfs-client.js";
import { AbortError, ForbiddenError, NotFoundError, TimeoutError } from "../src/errors.js";

describe("native VFS over a real HTTP connection", () => {
  let server: Server;
  let origin: string;
  let files: VfsClient;
  let fault = "";
  const contents = new Map<string, Uint8Array>();
  const calls: Array<{ method: string; path: string; query: URLSearchParams; headers: Record<string, unknown>; body: Uint8Array }> = [];
  const metadata = (path: string) => ({ path, size: contents.get(path)?.length ?? 0, mime_type: "application/octet-stream",
    is_directory: false, version: 1, gen: 2, zone_id: "shared", created_at_ms: null, modified_at_ms: 123, owner_id: "owner" });
  beforeAll(async () => {
    server = createServer(async (request, response) => {
      const url = new URL(request.url!, "http://localhost");
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const bytes = Buffer.concat(chunks);
      calls.push({ method: request.method!, path: url.pathname, query: url.searchParams, headers: request.headers, body: bytes });
      if (url.pathname === "/slow") { response.writeHead(200); response.flushHeaders(); return; }
      response.setHeader("content-type", "application/json");
      const path = url.searchParams.get("path") ?? "";
      const json = (body: unknown) => response.end(JSON.stringify(body));
      if (fault === "denied") { response.statusCode = 403; json({ error: "native permission denied" }); return; }
      if (fault === "text-error") { response.statusCode = 503; response.end("upstream unavailable"); return; }
      if (fault === "malformed") { json({}); return; }
      if (url.pathname === "/v2/directories") {
        json(request.method === "GET" ? { entries: [...contents.keys()].map(metadata) } : { hit: true }); return;
      }
      if (url.pathname === "/v2/files/metadata") { json(metadata(path)); return; }
      if (url.pathname === "/v2/files/rename") {
        const body = JSON.parse(bytes.toString());
        contents.set(body.new_path, contents.get(body.path)!); contents.delete(body.path);
        json({ success: true, is_directory: false }); return;
      }
      if (url.pathname === "/v2/files" && request.method === "PUT") {
        contents.set(path, bytes); json({ size: bytes.length, gen: 2, content_id: "native-content" }); return;
      }
      if (url.pathname === "/v2/files" && request.method === "DELETE") { json({ deleted: contents.delete(path) }); return; }
      if (url.pathname === "/v2/files" && contents.has(path)) {
        response.setHeader("content-type", "application/octet-stream"); response.end(contents.get(path)); return;
      }
      response.statusCode = 404; json({ error: "native path missing" });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("No HTTP port");
    origin = `http://127.0.0.1:${address.port}`;
  });
  afterAll(async () => {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });
  beforeEach(() => {
    fault = ""; contents.clear(); calls.length = 0;
    files = new VfsClient(new FetchClient({ apiKey: "native-test", baseUrl: origin, maxRetries: 0 }));
  });

  it("preserves Unicode paths, arbitrary bytes and literal error-looking file content", async () => {
    const path = '/日本語 & q="x"/bytes.bin';
    const bytes = new Uint8Array([0, 255, 254, 128, 13, 10]);
    expect(await files.write(path, bytes)).toEqual({ size: bytes.length, gen: 2, content_id: "native-content" });
    expect(await files.read(path)).toEqual(bytes);
    expect(calls[0]!.query.get("path")).toBe(path);
    expect(calls[0]!.headers["authorization"]).toBe("Bearer native-test");
    expect(calls[0]!.headers["content-type"]).toBe("application/octet-stream");
    const text = '{"error":"this is actual file content"}';
    await files.write(path, text);
    expect(new TextDecoder().decode(await files.read(path))).toBe(text);
  });

  it("uses native directory, metadata, rename and delete receipts", async () => {
    await files.mkdir("/docs");
    await files.write("/docs/old", "before");
    expect(await files.list("/docs")).toEqual([metadata("/docs/old")]);
    expect(await files.metadata("/docs/old")).toEqual(metadata("/docs/old"));
    await files.rename("/docs/old", "/docs/new");
    expect(new TextDecoder().decode(await files.read("/docs/new"))).toBe("before");
    expect(await files.delete("/docs/new", true)).toBe(true);
    expect(calls.at(-1)!.query.get("recursive")).toBe("true");
    expect(await files.delete("/docs/new")).toBe(false);
    expect(await files.list("/docs")).toEqual([]);
    expect(calls.find((call) => call.path.endsWith("/rename"))!.body.toString())
      .toContain('"new_path":"/docs/new"');
  });

  it.each(["list", "metadata", "write", "delete", "mkdir", "rename"] as const)("rejects incomplete %s acknowledgements", async (operation) => {
    fault = "malformed";
    const action = { list: () => files.list("/"), metadata: () => files.metadata("/x"), write: () => files.write("/x", "new"),
      delete: () => files.delete("/x"), mkdir: () => files.mkdir("/x"), rename: () => files.rename("/x", "/y") }[operation];
    await expect(action()).rejects.toThrow("Invalid native file response");
    expect(calls).toHaveLength(1);
  });

  it("preserves missing, denied and non-JSON upstream errors", async () => {
    await expect(files.read("/missing")).rejects.toBeInstanceOf(NotFoundError);
    fault = "denied";
    await expect(files.read("/private")).rejects.toBeInstanceOf(ForbiddenError);
    fault = "text-error";
    await expect(files.read("/broken")).rejects.toThrow("HTTP 503");
  });

  it("times out after headers when the response body stalls", async () => {
    const client = new FetchClient({ apiKey: "native-test", baseUrl: origin, timeout: 100 });
    await expect(client.rawRequest("GET", "/slow")).rejects.toBeInstanceOf(TimeoutError);
  });

  it("cancels a body transfer after headers", async () => {
    const controller = new AbortController();
    const client = new FetchClient({ apiKey: "native-test", baseUrl: origin, timeout: 5000 });
    const pending = client.rawRequest("GET", "/slow", undefined, { signal: controller.signal });
    const assertion = expect(pending).rejects.toBeInstanceOf(AbortError);
    await new Promise<void>((resolve) => server.once("request", () => resolve()));
    controller.abort();
    await assertion;
  });
});
