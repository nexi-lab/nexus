/**
 * Pure functions for generating code snippets from a RequestState.
 */

import type { RequestState } from "../../stores/api-console-store.js";

export type CodegenLanguage = "curl" | "fetch" | "python";

export function generateCode(
  lang: CodegenLanguage,
  request: RequestState,
  baseUrl: string,
): string {
  switch (lang) {
    case "curl":
      return generateCurl(request, baseUrl);
    case "fetch":
      return generateFetch(request, baseUrl);
    case "python":
      return generatePython(request, baseUrl);
  }
}

export function generateCurl(request: RequestState, baseUrl: string): string {
  const url = buildUrl(request, baseUrl);
  const parts = [`curl -X ${request.method} '${url}'`];

  for (const [key, value] of Object.entries(request.headers)) {
    parts.push(`  -H '${key}: ${value}'`);
  }

  if (request.body && request.method !== "GET" && request.method !== "HEAD") {
    parts.push(`  -H 'Content-Type: application/json'`);
    parts.push(`  -d '${request.body}'`);
  }

  return parts.join(" \\\n");
}

export function generateFetch(request: RequestState, baseUrl: string): string {
  const url = buildUrl(request, baseUrl);
  const lines: string[] = [];

  lines.push(`const response = await fetch('${url}', {`);
  lines.push(`  method: '${request.method}',`);

  const headers = { ...request.headers };
  if (request.body && request.method !== "GET" && request.method !== "HEAD") {
    headers["Content-Type"] = "application/json";
  }

  if (Object.keys(headers).length > 0) {
    lines.push("  headers: {");
    for (const [key, value] of Object.entries(headers)) {
      lines.push(`    '${key}': '${value}',`);
    }
    lines.push("  },");
  }

  if (request.body && request.method !== "GET" && request.method !== "HEAD") {
    lines.push(`  body: JSON.stringify(${request.body}),`);
  }

  lines.push("});");
  lines.push("");
  lines.push("const data = await response.json();");
  lines.push("console.log(data);");

  return lines.join("\n");
}

export function generatePython(request: RequestState, baseUrl: string): string {
  const url = buildUrl(request, baseUrl);
  const lines: string[] = [];

  lines.push("import httpx");
  lines.push("");

  if (Object.keys(request.headers).length > 0) {
    lines.push("headers = {");
    for (const [key, value] of Object.entries(request.headers)) {
      lines.push(`    "${key}": "${value}",`);
    }
    lines.push("}");
    lines.push("");
  }

  const method = request.method.toLowerCase();
  const hasBody = request.body && request.method !== "GET" && request.method !== "HEAD";
  const headerArg = Object.keys(request.headers).length > 0 ? ", headers=headers" : "";
  const bodyArg = hasBody ? `, json=${request.body}` : "";

  lines.push(`response = httpx.${method}("${url}"${headerArg}${bodyArg})`);
  lines.push("print(response.json())");

  return lines.join("\n");
}

function buildUrl(request: RequestState, baseUrl: string): string {
  let path = request.path;
  for (const [key, value] of Object.entries(request.pathParams)) {
    path = path.replace(`{${key}}`, encodeURIComponent(value));
  }

  const queryEntries = Object.entries(request.queryParams).filter(([, v]) => v !== "");
  if (queryEntries.length > 0) {
    const params = new URLSearchParams(queryEntries);
    path += `?${params.toString()}`;
  }

  return `${baseUrl}${path}`;
}
