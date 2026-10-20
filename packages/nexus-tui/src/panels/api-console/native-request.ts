export function parseNativeRequest(text: string): { method: string; path: string; body?: string; contentType: string } {
  const match = /^(GET|POST|PUT|PATCH|DELETE|HEAD)\s+(\/v2\/\S+)(?:\s+([\s\S]+))?$/.exec(text.trim());
  if (!match) throw new Error("Use METHOD /v2/path [JSON body]");
  const [, method, path, body] = match;
  const url = new URL(path!, "http://native");
  if (!url.pathname.startsWith("/v2/") || url.hash) throw new Error("Use a native /v2 path");
  if ((method === "GET" || method === "HEAD") && body !== undefined) throw new Error("GET and HEAD have no request body");
  const bytes = method === "PUT" && url.pathname === "/v2/files";
  if (body !== undefined && !bytes) JSON.parse(body);
  return { method: method!, path: path!, body, contentType: bytes ? "application/octet-stream" : "application/json" };
}
