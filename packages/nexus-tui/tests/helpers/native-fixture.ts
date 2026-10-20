export function createNativeFixture() {
  const files = new Map<string, Uint8Array>([["/notes.txt", new TextEncoder().encode("hello")]]);
  const directories = new Set(["/"]);
  const requests: Array<{ method: string; path: string; authorization: string | null }> = [];
  const control = { invalidWrite: false, writeGate: null as Promise<void> | null };
  const principal = { subject_type: "user", subject_id: "fixture-user", user_id: "fixture-user", agent_id: null,
    zone_id: "sharedzone", is_admin: true, zone_perms: [["sharedzone", "rw"]] };
  const metadata = (path: string) => ({ path, size: files.get(path)?.length ?? 0, mime_type: "text/plain",
    is_directory: directories.has(path), version: 1, gen: 1, zone_id: "sharedzone",
    created_at_ms: null, modified_at_ms: 1, owner_id: principal.user_id });
  const server = Bun.serve({ port: 0, hostname: "127.0.0.1", async fetch(request) {
    const url = new URL(request.url);
    requests.push({ method: request.method, path: url.pathname, authorization: request.headers.get("authorization") });
    if (url.pathname === "/v2/status") return Response.json({ status: "ok", version: "fixture-1" });
    if (request.headers.get("authorization") !== "Bearer tui-fixture") return Response.json({ error: "invalid native credentials" }, { status: 401 });
    if (url.pathname === "/v2/auth/whoami") return Response.json(principal);
    const path = url.searchParams.get("path") ?? "";
    if (url.pathname === "/v2/directories" && request.method === "GET") {
      const prefix = path === "/" ? "/" : `${path.replace(/\/$/, "")}/`;
      return Response.json({ entries: [...directories, ...files.keys()].filter((value) => value !== path && value.startsWith(prefix)
        && !value.slice(prefix.length).includes("/")).map(metadata) });
    }
    if (url.pathname === "/v2/directories" && request.method === "POST") {
      const body = await request.json(); directories.add(body.path); return Response.json({ hit: true });
    }
    if (url.pathname === "/v2/files/metadata") return Response.json(metadata(path));
    if (url.pathname === "/v2/files/rename") {
      const body = await request.json(); files.set(body.new_path, files.get(body.path)!); files.delete(body.path);
      return Response.json({ success: true, is_directory: false });
    }
    if (url.pathname === "/v2/files" && request.method === "PUT") {
      const bytes = new Uint8Array(await request.arrayBuffer());
      if (control.writeGate) await control.writeGate;
      if (control.invalidWrite) return Response.json({});
      files.set(path, bytes); return Response.json({ size: bytes.length, gen: 1, content_id: "fixture-content" });
    }
    if (url.pathname === "/v2/files" && request.method === "DELETE") return Response.json({ deleted: files.delete(path) || directories.delete(path) });
    if (url.pathname === "/v2/files" && files.has(path)) return new Response(files.get(path));
    if (url.pathname === "/v2/search/query") {
      const body = await request.json();
      if (body.q === "denied") return Response.json({ error: "search denied" }, { status: 403 });
      return Response.json({ results: [{ path: "/notes.txt", zone_id: body.zone_id, chunk_index: 0, chunk_text: body.q, score: 1 }] });
    }
    return Response.json({ error: "unsupported native fixture route" }, { status: 404 });
  } });
  return { url: server.url.origin, files, directories, requests, control, principal, stop: () => server.stop(true) };
}
