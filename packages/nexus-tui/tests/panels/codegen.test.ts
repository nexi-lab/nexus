import { describe, it, expect } from "bun:test";
import { generateCurl, generateFetch, generatePython, generateCode } from "../../src/panels/api-console/codegen.js";
import type { RequestState } from "../../src/stores/api-console-store.js";

const BASE_URL = "http://localhost:2026";

const GET_REQUEST: RequestState = {
  method: "GET",
  path: "/api/v2/files/list",
  pathParams: {},
  queryParams: { path: "/" },
  headers: { Authorization: "Bearer nx_live_agent1" },
  body: "",
};

const POST_REQUEST: RequestState = {
  method: "POST",
  path: "/api/v2/pay/transfer",
  pathParams: {},
  queryParams: {},
  headers: { Authorization: "Bearer nx_live_agent1" },
  body: '{"to": "agent-bob", "amount": "10.00"}',
};

const PATH_PARAM_REQUEST: RequestState = {
  method: "POST",
  path: "/api/v2/agents/{id}/evict",
  pathParams: { id: "agent-123" },
  queryParams: {},
  headers: {},
  body: "",
};

describe("generateCurl", () => {
  it("generates GET with query params", () => {
    const result = generateCurl(GET_REQUEST, BASE_URL);
    expect(result).toContain("curl -X GET");
    expect(result).toContain("http://localhost:2026/api/v2/files/list?path=%2F");
    expect(result).toContain("Authorization: Bearer nx_live_agent1");
  });

  it("generates POST with body", () => {
    const result = generateCurl(POST_REQUEST, BASE_URL);
    expect(result).toContain("curl -X POST");
    expect(result).toContain("-d '{\"to\": \"agent-bob\"");
    expect(result).toContain("Content-Type: application/json");
  });

  it("substitutes path parameters", () => {
    const result = generateCurl(PATH_PARAM_REQUEST, BASE_URL);
    expect(result).toContain("/api/v2/agents/agent-123/evict");
    expect(result).not.toContain("{id}");
  });
});

describe("generateFetch", () => {
  it("generates valid JavaScript", () => {
    const result = generateFetch(GET_REQUEST, BASE_URL);
    expect(result).toContain("const response = await fetch(");
    expect(result).toContain("method: 'GET'");
    expect(result).toContain("await response.json()");
  });

  it("includes body for POST", () => {
    const result = generateFetch(POST_REQUEST, BASE_URL);
    expect(result).toContain("body: JSON.stringify(");
    expect(result).toContain("Content-Type");
  });

  it("omits body for GET", () => {
    const result = generateFetch(GET_REQUEST, BASE_URL);
    expect(result).not.toContain("body:");
  });
});

describe("generatePython", () => {
  it("generates valid Python with httpx", () => {
    const result = generatePython(GET_REQUEST, BASE_URL);
    expect(result).toContain("import httpx");
    expect(result).toContain("httpx.get(");
    expect(result).toContain("headers=headers");
  });

  it("includes json body for POST", () => {
    const result = generatePython(POST_REQUEST, BASE_URL);
    expect(result).toContain("httpx.post(");
    expect(result).toContain("json=");
  });
});

describe("generateCode", () => {
  it("dispatches to correct generator", () => {
    const curl = generateCode("curl", GET_REQUEST, BASE_URL);
    const fetch = generateCode("fetch", GET_REQUEST, BASE_URL);
    const python = generateCode("python", GET_REQUEST, BASE_URL);

    expect(curl).toContain("curl");
    expect(fetch).toContain("fetch(");
    expect(python).toContain("httpx");
  });
});
