import { describe, it, expect, vi, beforeEach } from "vitest";
import { FetchClient } from "../src/fetch-client.js";
import {
  AuthenticationError,
  ForbiddenError,
  NotFoundError,
  ConflictError,
  RateLimitError,
  ServerError,
  NetworkError,
  TimeoutError,
  AbortError,
  NexusApiError,
} from "../src/errors.js";

function mockFetch(responses: Array<{ status: number; body?: unknown; headers?: Record<string, string> }>) {
  let callIndex = 0;
  return vi.fn(async () => {
    const resp = responses[callIndex] ?? responses[responses.length - 1]!;
    callIndex++;
    return new Response(
      resp.body !== undefined ? JSON.stringify(resp.body) : null,
      {
        status: resp.status,
        headers: {
          "Content-Type": "application/json",
          ...(resp.headers ?? {}),
        },
      },
    );
  }) as unknown as typeof globalThis.fetch;
}

describe("FetchClient", () => {
  let client: FetchClient;

  describe("HTTP methods", () => {
    it("GET request with auto camelCase transform", async () => {
      const fetchFn = mockFetch([{ status: 200, body: { from_agent: "a", to_agent: "b" } }]);
      client = new FetchClient({ apiKey: "test-key", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      const result = await client.get<{ fromAgent: string; toAgent: string }>("/api/v2/test");

      expect(result).toEqual({ fromAgent: "a", toAgent: "b" });
      expect(fetchFn).toHaveBeenCalledOnce();
      const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
      expect(url).toBe("http://localhost/api/v2/test");
      expect(init.method).toBe("GET");
      expect(init.headers.Authorization).toBe("Bearer test-key");
    });

    it("POST request transforms body to snake_case", async () => {
      const fetchFn = mockFetch([{ status: 200, body: { result_id: "123" } }]);
      client = new FetchClient({ apiKey: "test-key", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await client.post("/api/v2/test", { fromAgent: "a", toAgent: "b" });

      const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
      expect(JSON.parse(init.body)).toEqual({ from_agent: "a", to_agent: "b" });
      expect(init.headers["Content-Type"]).toBe("application/json");
    });

    it("PUT request", async () => {
      const fetchFn = mockFetch([{ status: 200, body: {} }]);
      client = new FetchClient({ apiKey: "test-key", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await client.put("/api/v2/test", { name: "updated" });

      const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
      expect(init.method).toBe("PUT");
    });

    it("DELETE request", async () => {
      const fetchFn = mockFetch([{ status: 200, body: {} }]);
      client = new FetchClient({ apiKey: "test-key", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await client.delete("/api/v2/test");

      const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
      expect(init.method).toBe("DELETE");
    });

    it("postNoContent handles 204", async () => {
      const fetchFn = mockFetch([{ status: 204 }]);
      client = new FetchClient({ apiKey: "test-key", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await expect(client.postNoContent("/api/v2/test")).resolves.toBeUndefined();
    });

    it("deleteNoContent handles 204", async () => {
      const fetchFn = mockFetch([{ status: 204 }]);
      client = new FetchClient({ apiKey: "test-key", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await expect(client.deleteNoContent("/api/v2/test")).resolves.toBeUndefined();
    });
  });

  describe("transformKeys: false", () => {
    it("disables key transformation", async () => {
      const fetchFn = mockFetch([{ status: 200, body: { from_agent: "a" } }]);
      client = new FetchClient({ apiKey: "test-key", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0, transformKeys: false });

      const result = await client.get<{ from_agent: string }>("/test");
      expect(result).toEqual({ from_agent: "a" });
    });
  });

  describe("error mapping", () => {
    const makeClient = (status: number, body?: unknown, headers?: Record<string, string>) => {
      const fetchFn = mockFetch([{ status, body, headers }]);
      return new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });
    };

    it("401 → AuthenticationError", async () => {
      client = makeClient(401, { detail: "Invalid key" });
      await expect(client.get("/test")).rejects.toThrow(AuthenticationError);
    });

    it("403 → ForbiddenError", async () => {
      client = makeClient(403, { detail: "Denied" });
      await expect(client.get("/test")).rejects.toThrow(ForbiddenError);
    });

    it("404 → NotFoundError", async () => {
      client = makeClient(404, { detail: "Not found" });
      await expect(client.get("/test")).rejects.toThrow(NotFoundError);
    });

    it("409 → ConflictError", async () => {
      client = makeClient(409, { detail: "Conflict" });
      await expect(client.get("/test")).rejects.toThrow(ConflictError);
    });

    it("429 → RateLimitError with retryAfter", async () => {
      client = makeClient(429, { detail: "Too many" }, { "Retry-After": "30" });
      try {
        await client.get("/test");
        expect.fail("should throw");
      } catch (e) {
        expect(e).toBeInstanceOf(RateLimitError);
        expect((e as RateLimitError).retryAfter).toBe(30);
      }
    });

    it("500 → ServerError", async () => {
      client = makeClient(500, { detail: "Internal" });
      await expect(client.get("/test")).rejects.toThrow(ServerError);
    });

    it("502 → ServerError", async () => {
      client = makeClient(502, { detail: "Bad gateway" });
      await expect(client.get("/test")).rejects.toThrow(ServerError);
    });

    it("unknown 4xx → NexusApiError", async () => {
      client = makeClient(418, { detail: "Teapot" });
      await expect(client.get("/test")).rejects.toThrow(NexusApiError);
    });

    it("handles non-JSON error body", async () => {
      const fetchFn = vi.fn(async () => new Response("plain text", { status: 400 })) as unknown as typeof globalThis.fetch;
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });
      await expect(client.get("/test")).rejects.toThrow("HTTP 400");
    });
  });

  describe("retry logic", () => {
    it("retries on 500 and succeeds on second attempt", async () => {
      const fetchFn = mockFetch([
        { status: 500, body: { detail: "error" } },
        { status: 200, body: { result: "ok" } },
      ]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 1 });

      const result = await client.get<{ result: string }>("/test");
      expect(result).toEqual({ result: "ok" });
      expect(fetchFn).toHaveBeenCalledTimes(2);
    });

    it("retries on 429", async () => {
      const fetchFn = mockFetch([
        { status: 429, body: { detail: "throttled" } },
        { status: 200, body: {} },
      ]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 1 });

      await client.get("/test");
      expect(fetchFn).toHaveBeenCalledTimes(2);
    });

    it("does NOT retry on 401", async () => {
      const fetchFn = mockFetch([
        { status: 401, body: { detail: "bad" } },
      ]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 3 });

      await expect(client.get("/test")).rejects.toThrow(AuthenticationError);
      expect(fetchFn).toHaveBeenCalledTimes(1);
    });

    it("throws after retries exhausted", async () => {
      const fetchFn = mockFetch([
        { status: 500, body: { detail: "fail" } },
        { status: 500, body: { detail: "fail" } },
        { status: 500, body: { detail: "fail" } },
      ]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 2 });

      await expect(client.get("/test")).rejects.toThrow(ServerError);
      expect(fetchFn).toHaveBeenCalledTimes(3);
    });

    it("retries on network error (TypeError from fetch)", async () => {
      let callCount = 0;
      const fetchFn = vi.fn(async () => {
        callCount++;
        if (callCount === 1) throw new TypeError("Failed to fetch");
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }) as unknown as typeof globalThis.fetch;
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 1 });

      const result = await client.get<{ ok: boolean }>("/test");
      expect(result).toEqual({ ok: true });
      expect(fetchFn).toHaveBeenCalledTimes(2);
    });
  });

  describe("headers", () => {
    it("sends Authorization header", async () => {
      const fetchFn = mockFetch([{ status: 200, body: {} }]);
      client = new FetchClient({ apiKey: "my-key", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await client.get("/test");

      const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
      expect(init.headers.Authorization).toBe("Bearer my-key");
    });

    it("sends idempotency key when provided", async () => {
      const fetchFn = mockFetch([{ status: 200, body: {} }]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await client.get("/test", { idempotencyKey: "idem-123" });

      const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
      expect(init.headers["Idempotency-Key"]).toBe("idem-123");
    });

    it("merges extra headers", async () => {
      const fetchFn = mockFetch([{ status: 200, body: {} }]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await client.get("/test", {
        headers: { "X-Agent-ID": "bot-1", "X-Nexus-Zone-ID": "org_acme" },
      });

      const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
      expect(init.headers["X-Agent-ID"]).toBe("bot-1");
      expect(init.headers["X-Nexus-Zone-ID"]).toBe("org_acme");
    });
  });

  describe("PATCH method", () => {
    it("sends PATCH request with body", async () => {
      const fetchFn = mockFetch([{ status: 200, body: { updated: true } }]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      const result = await client.patch<{ updated: boolean }>("/api/v2/resource", { name: "new" });

      expect(result).toEqual({ updated: true });
      const [, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
      expect(init.method).toBe("PATCH");
      expect(init.headers["Content-Type"]).toBe("application/json");
    });
  });

  describe("rawRequest", () => {
    it("returns raw Response without JSON parsing", async () => {
      const fetchFn = vi.fn(async () =>
        new Response(JSON.stringify({ raw: true }), { status: 200 }),
      ) as unknown as typeof globalThis.fetch;
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      const response = await client.rawRequest("GET", "/api/v2/test");

      expect(response).toBeInstanceOf(Response);
      expect(response.status).toBe(200);
      const body = await response.json();
      expect(body).toEqual({ raw: true });
    });

    it("sends body as-is without JSON.stringify", async () => {
      const fetchFn = vi.fn(async () =>
        new Response("ok", { status: 200 }),
      ) as unknown as typeof globalThis.fetch;
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await client.rawRequest("POST", "/api/v2/test", '{"already":"stringified"}');

      const [, init] = fetchFn.mock.calls[0]!;
      expect(init.body).toBe('{"already":"stringified"}');
    });

    it("throws AbortError when signal is already aborted", async () => {
      const fetchFn = vi.fn(async () =>
        new Response("ok", { status: 200 }),
      ) as unknown as typeof globalThis.fetch;
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      const controller = new AbortController();
      controller.abort();

      await expect(
        client.rawRequest("GET", "/test", undefined, { signal: controller.signal }),
      ).rejects.toThrow(AbortError);
    });

    it("throws TimeoutError on fetch timeout", async () => {
      const fetchFn = vi.fn(async () => {
        throw new DOMException("The operation was aborted", "AbortError");
      }) as unknown as typeof globalThis.fetch;
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0, timeout: 100 });

      await expect(client.rawRequest("GET", "/test")).rejects.toThrow(TimeoutError);
    });

    it("throws AbortError when user signal fires during fetch", async () => {
      const controller = new AbortController();
      const fetchFn = vi.fn(async (_url: string, init: { signal: AbortSignal }) => {
        // Simulate user aborting mid-request
        controller.abort();
        // Throw AbortError as fetch would
        throw new DOMException("Aborted", "AbortError");
      }) as unknown as typeof globalThis.fetch;
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await expect(
        client.rawRequest("GET", "/test", undefined, { signal: controller.signal }),
      ).rejects.toThrow(AbortError);
    });

    it("sends identity headers from config", async () => {
      const fetchFn = vi.fn(async () =>
        new Response("ok", { status: 200 }),
      ) as unknown as typeof globalThis.fetch;
      client = new FetchClient({
        apiKey: "k",
        baseUrl: "http://localhost",
        fetch: fetchFn,
        maxRetries: 0,
        agentId: "bot-1",
        subject: "user:bob",
        zoneId: "zone-x",
      });

      await client.rawRequest("GET", "/test");

      const [, init] = fetchFn.mock.calls[0]!;
      expect(init.headers["X-Agent-ID"]).toBe("bot-1");
      expect(init.headers["X-Nexus-Subject"]).toBe("user:bob");
      expect(init.headers["X-Nexus-Zone-ID"]).toBe("zone-x");
    });
  });

  describe("timeout and abort", () => {
    it("throws AbortError when signal is already aborted", async () => {
      const fetchFn = mockFetch([{ status: 200, body: {} }]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      const controller = new AbortController();
      controller.abort();

      await expect(client.get("/test", { signal: controller.signal })).rejects.toThrow(AbortError);
    });

    it("throws TimeoutError when fetch aborts due to timeout", async () => {
      const fetchFn = vi.fn(async () => {
        throw new DOMException("The operation was aborted", "AbortError");
      }) as unknown as typeof globalThis.fetch;
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0, timeout: 100 });

      await expect(client.get("/test")).rejects.toThrow(TimeoutError);
    });

    it("throws AbortError when user aborts during fetch", async () => {
      const controller = new AbortController();
      const fetchFn = vi.fn(async () => {
        controller.abort();
        throw new DOMException("Aborted", "AbortError");
      }) as unknown as typeof globalThis.fetch;
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await expect(
        client.get("/test", { signal: controller.signal }),
      ).rejects.toThrow(AbortError);
    });

    it("cleans up user signal listener after request", async () => {
      const fetchFn = mockFetch([{ status: 200, body: { ok: true } }]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      const controller = new AbortController();
      const removeSpy = vi.spyOn(controller.signal, "removeEventListener");

      await client.get("/test", { signal: controller.signal });
      expect(removeSpy).toHaveBeenCalledWith("abort", expect.any(Function));
    });
  });

  describe("retry with RateLimitError retryAfter", () => {
    it("uses retryAfter from 429 header for retry delay", async () => {
      const fetchFn = mockFetch([
        { status: 429, body: { detail: "throttled" }, headers: { "Retry-After": "2" } },
        { status: 200, body: { ok: true } },
      ]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 1 });

      const result = await client.get<{ ok: boolean }>("/test");
      expect(result).toEqual({ ok: true });
      expect(fetchFn).toHaveBeenCalledTimes(2);
    });
  });

  describe("204 response handling", () => {
    it("returns undefined for 204 responses", async () => {
      const fetchFn = mockFetch([{ status: 204 }]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      const result = await client.get("/test");
      expect(result).toBeUndefined();
    });
  });

  describe("network errors exhaust retries", () => {
    it("wraps non-NexusApiError in NetworkError after exhausting retries", async () => {
      const fetchFn = vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }) as unknown as typeof globalThis.fetch;
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 1 });

      await expect(client.get("/test")).rejects.toThrow(NetworkError);
      expect(fetchFn).toHaveBeenCalledTimes(2);
    });
  });

  describe("getAspect", () => {
    it("returns null on 404 only", async () => {
      const fetchFn = mockFetch([{ status: 404, body: { detail: "Not found" } }]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await expect(client.getAspect("urn:li:dataset:test", "schema")).resolves.toBeNull();
    });

    it("rethrows non-404 errors", async () => {
      const fetchFn = mockFetch([{ status: 401, body: { detail: "Unauthorized" } }]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      await expect(client.getAspect("urn:li:dataset:test", "schema")).rejects.toThrow(AuthenticationError);
    });
  });

  describe("abort compatibility", () => {
    it("maps non-DOM abort errors to TimeoutError", async () => {
      const fetchFn = vi.fn(async () => {
        const error = new Error("aborted");
        Object.assign(error, { name: "AbortError" });
        throw error;
      }) as unknown as typeof globalThis.fetch;
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0, timeout: 100 });

      await expect(client.get("/test")).rejects.toThrow(TimeoutError);
    });
  });

  describe("base URL handling", () => {
    it("strips trailing slashes", async () => {
      const fetchFn = mockFetch([{ status: 200, body: {} }]);
      client = new FetchClient({ apiKey: "k", baseUrl: "http://localhost:2026///", fetch: fetchFn, maxRetries: 0 });

      await client.get("/test");

      const [url] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
      expect(url).toBe("http://localhost:2026/test");
    });

    it("uses default base URL when not provided", async () => {
      const fetchFn = mockFetch([{ status: 200, body: {} }]);
      client = new FetchClient({ apiKey: "k", fetch: fetchFn, maxRetries: 0 });

      await client.get("/test");

      const [url] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
      expect(url).toBe("http://localhost:2026/test");
    });
  });
});
