import { type Mock, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FetchClient } from "../src/fetch-client.js";
import {
  AuthenticationError,
  BudgetExceededError,
  InsufficientCreditsError,
  NexusPayError,
  RateLimitError,
  ReservationError,
  WalletNotFoundError,
} from "../src/errors.js";

function jsonResponse(body: unknown, status = 200, headers?: Record<string, string>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function textResponse(body: string, status = 200): Response {
  return new Response(body, { status, headers: { "Content-Type": "text/plain" } });
}

describe("FetchClient", () => {
  let mockFetch: Mock;
  let client: FetchClient;

  beforeEach(() => {
    mockFetch = vi.fn();
    client = new FetchClient({
      apiKey: "nx_test_agent1",
      baseUrl: "https://api.example.com",
      timeout: 5000,
      maxRetries: 0,
      fetch: mockFetch,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // =========================================================================
  // Auth header
  // =========================================================================

  describe("auth header", () => {
    it("attaches Bearer token from API key", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
      await client.get("/test");

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      const headers = new Headers(init.headers);
      expect(headers.get("Authorization")).toBe("Bearer nx_test_agent1");
    });

    it("sets Content-Type for POST requests", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
      await client.post("/test", { data: 1 });

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      const headers = new Headers(init.headers);
      expect(headers.get("Content-Type")).toBe("application/json");
    });
  });

  // =========================================================================
  // URL construction
  // =========================================================================

  describe("URL construction", () => {
    it("joins base URL with path", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
      await client.get("/api/v2/pay/balance");

      const [url] = mockFetch.mock.calls[0] as [string];
      expect(url).toBe("https://api.example.com/api/v2/pay/balance");
    });

    it("handles base URL with trailing slash", async () => {
      const c = new FetchClient({
        apiKey: "nx_test_x",
        baseUrl: "https://api.example.com/",
        maxRetries: 0,
        fetch: mockFetch,
      });
      mockFetch.mockResolvedValueOnce(jsonResponse({ ok: true }));
      await c.get("/api/v2/pay/balance");

      const [url] = mockFetch.mock.calls[0] as [string];
      expect(url).toBe("https://api.example.com/api/v2/pay/balance");
    });
  });

  // =========================================================================
  // GET / POST / POST-no-content
  // =========================================================================

  describe("request methods", () => {
    it("GET sends no body", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ value: 42 }));
      const result = await client.get<{ value: number }>("/data");

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      expect(init.method).toBe("GET");
      expect(init.body).toBeUndefined();
      expect(result).toEqual({ value: 42 });
    });

    it("POST sends JSON body", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ id: "tx_1" }));
      const result = await client.post<{ id: string }>("/transfer", { to: "bob", amount: "10" });

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      expect(init.method).toBe("POST");
      expect(init.body).toBe(JSON.stringify({ to: "bob", amount: "10" }));
      expect(result).toEqual({ id: "tx_1" });
    });

    it("postNoContent handles 204 response", async () => {
      mockFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
      await expect(client.postNoContent("/commit", { amount: "5" })).resolves.toBeUndefined();
    });

    it("postNoContent handles 200 with empty body", async () => {
      mockFetch.mockResolvedValueOnce(new Response(null, { status: 200 }));
      await expect(client.postNoContent("/commit")).resolves.toBeUndefined();
    });
  });

  // =========================================================================
  // Error mapping (HTTP status → typed error)
  // =========================================================================

  describe("error mapping", () => {
    it("401 → AuthenticationError", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "Invalid token", error_code: "auth_error" }, 401),
      );
      await expect(client.get("/test")).rejects.toThrow(AuthenticationError);
    });

    it("402 → InsufficientCreditsError", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "Not enough", error_code: "insufficient_credits" }, 402),
      );
      await expect(client.get("/test")).rejects.toThrow(InsufficientCreditsError);
    });

    it("403 → BudgetExceededError", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "Over budget", error_code: "budget_exceeded" }, 403),
      );
      await expect(client.get("/test")).rejects.toThrow(BudgetExceededError);
    });

    it("404 → WalletNotFoundError", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "No wallet", error_code: "wallet_not_found" }, 404),
      );
      await expect(client.get("/test")).rejects.toThrow(WalletNotFoundError);
    });

    it("409 → ReservationError", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "Conflict", error_code: "reservation_error" }, 409),
      );
      await expect(client.get("/test")).rejects.toThrow(ReservationError);
    });

    it("429 → RateLimitError with retryAfter", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "Slow down" }, 429, { "Retry-After": "30" }),
      );
      try {
        await client.get("/test");
        expect.fail("should have thrown");
      } catch (e) {
        expect(e).toBeInstanceOf(RateLimitError);
        expect((e as RateLimitError).retryAfter).toBe(30);
      }
    });

    it("429 → RateLimitError without Retry-After header", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ detail: "Slow down" }, 429));
      try {
        await client.get("/test");
        expect.fail("should have thrown");
      } catch (e) {
        expect(e).toBeInstanceOf(RateLimitError);
        expect((e as RateLimitError).retryAfter).toBeUndefined();
      }
    });

    it("other 4xx → NexusPayError", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "Bad request" }, 400),
      );
      try {
        await client.get("/test");
        expect.fail("should have thrown");
      } catch (e) {
        expect(e).toBeInstanceOf(NexusPayError);
        expect((e as NexusPayError).status).toBe(400);
      }
    });

    it("5xx → NexusPayError", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "Server error" }, 500),
      );
      await expect(client.get("/test")).rejects.toThrow(NexusPayError);
    });

    it("non-JSON error response uses status text", async () => {
      mockFetch.mockResolvedValueOnce(textResponse("Internal Server Error", 500));
      try {
        await client.get("/test");
        expect.fail("should have thrown");
      } catch (e) {
        expect(e).toBeInstanceOf(NexusPayError);
        expect((e as NexusPayError).message).toContain("500");
      }
    });

    it("error includes detail from JSON response", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "Insufficient balance for transfer of 100 credits" }, 402),
      );
      try {
        await client.get("/test");
        expect.fail("should have thrown");
      } catch (e) {
        expect(e).toBeInstanceOf(InsufficientCreditsError);
        expect((e as NexusPayError).message).toBe(
          "Insufficient balance for transfer of 100 credits",
        );
      }
    });
  });

  // =========================================================================
  // Retry logic
  // =========================================================================

  describe("retry", () => {
    let retryClient: FetchClient;

    beforeEach(() => {
      retryClient = new FetchClient({
        apiKey: "nx_test_agent1",
        baseUrl: "https://api.example.com",
        timeout: 5000,
        maxRetries: 2,
        fetch: mockFetch,
      });
    });

    it("retries on 500 and succeeds", async () => {
      mockFetch
        .mockResolvedValueOnce(jsonResponse({ detail: "err" }, 500))
        .mockResolvedValueOnce(jsonResponse({ ok: true }));

      const result = await retryClient.get<{ ok: boolean }>("/test");
      expect(result.ok).toBe(true);
      expect(mockFetch).toHaveBeenCalledTimes(2);
    });

    it("retries on 502, 503, 504", async () => {
      mockFetch
        .mockResolvedValueOnce(jsonResponse({ detail: "err" }, 502))
        .mockResolvedValueOnce(jsonResponse({ detail: "err" }, 503))
        .mockResolvedValueOnce(jsonResponse({ ok: true }));

      const result = await retryClient.get<{ ok: boolean }>("/test");
      expect(result.ok).toBe(true);
      expect(mockFetch).toHaveBeenCalledTimes(3);
    });

    it("throws after max retries exhausted on 500", async () => {
      mockFetch
        .mockResolvedValue(jsonResponse({ detail: "server down" }, 500));

      await expect(retryClient.get("/test")).rejects.toThrow(NexusPayError);
      // 1 initial + 2 retries = 3 calls
      expect(mockFetch).toHaveBeenCalledTimes(3);
    });

    it("does NOT retry on 400", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ detail: "bad" }, 400));
      await expect(retryClient.get("/test")).rejects.toThrow(NexusPayError);
      expect(mockFetch).toHaveBeenCalledTimes(1);
    });

    it("does NOT retry on 401", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ detail: "unauth" }, 401));
      await expect(retryClient.get("/test")).rejects.toThrow(AuthenticationError);
      expect(mockFetch).toHaveBeenCalledTimes(1);
    });

    it("does NOT retry on 402", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ detail: "no credits" }, 402));
      await expect(retryClient.get("/test")).rejects.toThrow(InsufficientCreditsError);
      expect(mockFetch).toHaveBeenCalledTimes(1);
    });

    it("does NOT retry on 403", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ detail: "forbidden" }, 403));
      await expect(retryClient.get("/test")).rejects.toThrow(BudgetExceededError);
      expect(mockFetch).toHaveBeenCalledTimes(1);
    });

    it("does NOT retry on 404", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ detail: "not found" }, 404));
      await expect(retryClient.get("/test")).rejects.toThrow(WalletNotFoundError);
      expect(mockFetch).toHaveBeenCalledTimes(1);
    });

    it("retries on 429 and respects Retry-After", async () => {
      mockFetch
        .mockResolvedValueOnce(
          jsonResponse({ detail: "rate limited" }, 429, { "Retry-After": "0" }),
        )
        .mockResolvedValueOnce(jsonResponse({ ok: true }));

      const result = await retryClient.get<{ ok: boolean }>("/test");
      expect(result.ok).toBe(true);
      expect(mockFetch).toHaveBeenCalledTimes(2);
    });

    it("retries on network error (fetch rejects)", async () => {
      mockFetch
        .mockRejectedValueOnce(new TypeError("fetch failed"))
        .mockResolvedValueOnce(jsonResponse({ ok: true }));

      const result = await retryClient.get<{ ok: boolean }>("/test");
      expect(result.ok).toBe(true);
      expect(mockFetch).toHaveBeenCalledTimes(2);
    });

    it("throws NexusPayError after network error retries exhausted", async () => {
      mockFetch.mockRejectedValue(new TypeError("fetch failed"));

      await expect(retryClient.get("/test")).rejects.toThrow(NexusPayError);
      expect(mockFetch).toHaveBeenCalledTimes(3);
    });
  });

  // =========================================================================
  // Timeout
  // =========================================================================

  describe("timeout", () => {
    it("aborts request on timeout", async () => {
      const slowClient = new FetchClient({
        apiKey: "nx_test_x",
        baseUrl: "https://api.example.com",
        timeout: 50, // 50ms timeout
        maxRetries: 0,
        fetch: mockFetch,
      });

      mockFetch.mockImplementationOnce(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            // Simulate slow response — listen for abort
            init.signal?.addEventListener("abort", () => {
              reject(new DOMException("The operation was aborted.", "AbortError"));
            });
          }),
      );

      await expect(slowClient.get("/slow")).rejects.toThrow(NexusPayError);
    });

    it("per-request timeout overrides global", async () => {
      // Global timeout is 5000ms but per-request is 50ms
      mockFetch.mockImplementationOnce(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () => {
              reject(new DOMException("The operation was aborted.", "AbortError"));
            });
          }),
      );

      await expect(client.get("/slow", { timeout: 50 })).rejects.toThrow(NexusPayError);
    });

    it("user-provided AbortSignal is respected", async () => {
      const controller = new AbortController();

      mockFetch.mockImplementationOnce(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener("abort", () => {
              reject(new DOMException("The operation was aborted.", "AbortError"));
            });
          }),
      );

      // Abort immediately
      controller.abort();

      await expect(client.get("/test", { signal: controller.signal })).rejects.toThrow(
        NexusPayError,
      );
    });
  });

  // =========================================================================
  // Idempotency key
  // =========================================================================

  describe("idempotency key", () => {
    it("sends Idempotency-Key header when provided", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ id: "tx_1" }));
      await client.post("/transfer", { to: "bob" }, { idempotencyKey: "key-123" });

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      const headers = new Headers(init.headers);
      expect(headers.get("Idempotency-Key")).toBe("key-123");
    });

    it("omits Idempotency-Key header when not provided", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ id: "tx_1" }));
      await client.post("/transfer", { to: "bob" });

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      const headers = new Headers(init.headers);
      expect(headers.get("Idempotency-Key")).toBeNull();
    });
  });
});
