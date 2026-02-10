import { type Mock, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NexusPay } from "../src/client.js";
import {
  BudgetExceededError,
  InsufficientCreditsError,
  NexusPayError,
  ReservationError,
} from "../src/errors.js";
import type { Balance, CanAffordResult, MeterResult, Receipt, Reservation } from "../src/types.js";

// =============================================================================
// Test helpers
// =============================================================================

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Wire-format balance (snake_case). */
const BALANCE_API = { available: "100.50", reserved: "5.00", total: "105.50" };

/** Wire-format receipt (snake_case). */
const RECEIPT_API = {
  id: "tx_001",
  method: "credits",
  amount: "10.00",
  from_agent: "agent1",
  to_agent: "agent-bob",
  memo: "Task payment",
  timestamp: "2025-06-01T12:00:00Z",
  tx_hash: null,
};

/** Wire-format reservation (snake_case). */
const RESERVATION_API = {
  id: "res_001",
  amount: "25.00",
  purpose: "compute",
  expires_at: null,
  status: "pending",
};

// =============================================================================
// Constructor tests
// =============================================================================

describe("NexusPay constructor", () => {
  const mockFetch = vi.fn();

  it("accepts nx_live_* key and extracts agentId", () => {
    const pay = new NexusPay({ apiKey: "nx_live_myagent", fetch: mockFetch });
    expect(pay.agentId).toBe("myagent");
  });

  it("accepts nx_test_* key and extracts agentId", () => {
    const pay = new NexusPay({ apiKey: "nx_test_agent42", fetch: mockFetch });
    expect(pay.agentId).toBe("agent42");
  });

  it("accepts sk-* keys (standard API keys)", () => {
    const pay = new NexusPay({ apiKey: "sk-default_admin_abc123", fetch: mockFetch });
    expect(pay.agentId).toBeUndefined();
  });

  it("accepts any non-empty key string", () => {
    const pay = new NexusPay({ apiKey: "my-custom-key", fetch: mockFetch });
    expect(pay.agentId).toBeUndefined();
  });

  it("throws on empty string", () => {
    expect(() => new NexusPay({ apiKey: "", fetch: mockFetch })).toThrow(NexusPayError);
  });

  it("uses default baseUrl when not provided", () => {
    const pay = new NexusPay({ apiKey: "nx_test_x", fetch: mockFetch });
    expect(pay.agentId).toBe("x");
  });

  it("accepts custom baseUrl", () => {
    const pay = new NexusPay({
      apiKey: "nx_test_x",
      baseUrl: "http://localhost:2026",
      fetch: mockFetch,
    });
    expect(pay.agentId).toBe("x");
  });
});

// =============================================================================
// Method tests
// =============================================================================

describe("NexusPay methods", () => {
  let mockFetch: Mock;
  let pay: NexusPay;

  beforeEach(() => {
    mockFetch = vi.fn();
    pay = new NexusPay({
      apiKey: "nx_test_agent1",
      baseUrl: "https://api.example.com",
      maxRetries: 0,
      fetch: mockFetch,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // =========================================================================
  // getBalance
  // =========================================================================

  describe("getBalance", () => {
    it("returns Balance with camelCase fields", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse(BALANCE_API));
      const balance = await pay.getBalance();

      expect(balance).toEqual<Balance>({
        available: "100.50",
        reserved: "5.00",
        total: "105.50",
      });
    });

    it("calls GET /api/v2/pay/balance", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse(BALANCE_API));
      await pay.getBalance();

      const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("https://api.example.com/api/v2/pay/balance");
      expect(init.method).toBe("GET");
    });

    it("passes request options through", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse(BALANCE_API));
      await pay.getBalance({ timeout: 5000 });
      // Doesn't throw — timeout option accepted
    });
  });

  // =========================================================================
  // canAfford
  // =========================================================================

  describe("canAfford", () => {
    it("returns CanAffordResult", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ can_afford: true, amount: "50.00" }),
      );
      const result = await pay.canAfford("50.00");

      expect(result).toEqual<CanAffordResult>({ canAfford: true, amount: "50.00" });
    });

    it("sends amount as query parameter", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ can_afford: false, amount: "999.00" }),
      );
      await pay.canAfford("999.00");

      const [url] = mockFetch.mock.calls[0] as [string];
      expect(url).toContain("/api/v2/pay/can-afford?amount=999.00");
    });

    it("validates amount is not empty", async () => {
      await expect(pay.canAfford("")).rejects.toThrow(NexusPayError);
    });
  });

  // =========================================================================
  // transfer
  // =========================================================================

  describe("transfer", () => {
    it("returns Receipt with camelCase fields", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse(RECEIPT_API, 201));
      const receipt = await pay.transfer({ to: "agent-bob", amount: "10.00", memo: "Task" });

      expect(receipt).toEqual<Receipt>({
        id: "tx_001",
        method: "credits",
        amount: "10.00",
        fromAgent: "agent1",
        toAgent: "agent-bob",
        memo: "Task payment",
        timestamp: "2025-06-01T12:00:00Z",
        txHash: null,
      });
    });

    it("sends POST /api/v2/pay/transfer with body", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse(RECEIPT_API, 201));
      await pay.transfer({ to: "bob", amount: "5.00", memo: "test" });

      const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("https://api.example.com/api/v2/pay/transfer");
      expect(init.method).toBe("POST");

      const body = JSON.parse(init.body as string);
      expect(body.to).toBe("bob");
      expect(body.amount).toBe("5.00");
      expect(body.memo).toBe("test");
    });

    it("sends method field when specified", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse(RECEIPT_API, 201));
      await pay.transfer({ to: "0x1234", amount: "1.00", method: "x402" });

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      const body = JSON.parse(init.body as string);
      expect(body.method).toBe("x402");
    });

    it("passes idempotency key to request options", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse(RECEIPT_API, 201));
      await pay.transfer(
        { to: "bob", amount: "10.00" },
        { idempotencyKey: "idem-key-1" },
      );

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      const headers = new Headers(init.headers);
      expect(headers.get("Idempotency-Key")).toBe("idem-key-1");
    });

    it("handles 402 InsufficientCreditsError", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "Insufficient balance" }, 402),
      );
      await expect(
        pay.transfer({ to: "bob", amount: "9999.00" }),
      ).rejects.toThrow(InsufficientCreditsError);
    });

    it("validates amount is positive", async () => {
      await expect(
        pay.transfer({ to: "bob", amount: "-1.00" }),
      ).rejects.toThrow(NexusPayError);
    });

    it("validates amount is a valid decimal", async () => {
      await expect(
        pay.transfer({ to: "bob", amount: "abc" }),
      ).rejects.toThrow(NexusPayError);
    });

    it("validates 'to' is not empty", async () => {
      await expect(
        pay.transfer({ to: "", amount: "1.00" }),
      ).rejects.toThrow(NexusPayError);
    });

    it("validates amount has at most 6 decimal places", async () => {
      await expect(
        pay.transfer({ to: "bob", amount: "1.1234567" }),
      ).rejects.toThrow(NexusPayError);
    });
  });

  // =========================================================================
  // transferBatch
  // =========================================================================

  describe("transferBatch", () => {
    it("returns array of Receipts", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse([RECEIPT_API], 201));
      const receipts = await pay.transferBatch([
        { to: "agent-bob", amount: "10.00", memo: "Task" },
      ]);

      expect(receipts).toHaveLength(1);
      expect(receipts[0]?.fromAgent).toBe("agent1");
    });

    it("sends POST /api/v2/pay/transfer/batch", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse([], 201));
      await pay.transferBatch([]);

      const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("https://api.example.com/api/v2/pay/transfer/batch");
      expect(init.method).toBe("POST");
    });

    it("sends transfers in body", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse([RECEIPT_API, RECEIPT_API], 201));
      await pay.transferBatch([
        { to: "alice", amount: "5.00" },
        { to: "bob", amount: "3.00", memo: "bonus" },
      ]);

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      const body = JSON.parse(init.body as string);
      expect(body.transfers).toHaveLength(2);
      expect(body.transfers[0].to).toBe("alice");
      expect(body.transfers[1].memo).toBe("bonus");
    });

    it("validates batch size <= 1000", async () => {
      const bigBatch = Array.from({ length: 1001 }, (_, i) => ({
        to: `agent-${i}`,
        amount: "1.00",
      }));
      await expect(pay.transferBatch(bigBatch)).rejects.toThrow(NexusPayError);
    });
  });

  // =========================================================================
  // reserve
  // =========================================================================

  describe("reserve", () => {
    it("returns Reservation with camelCase fields", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse(RESERVATION_API, 201));
      const reservation = await pay.reserve({ amount: "25.00", purpose: "compute" });

      expect(reservation).toEqual<Reservation>({
        id: "res_001",
        amount: "25.00",
        purpose: "compute",
        expiresAt: null,
        status: "pending",
      });
    });

    it("sends POST /api/v2/pay/reserve", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse(RESERVATION_API, 201));
      await pay.reserve({ amount: "10.00" });

      const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("https://api.example.com/api/v2/pay/reserve");
      expect(init.method).toBe("POST");
    });

    it("includes optional fields in body", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse(RESERVATION_API, 201));
      await pay.reserve({ amount: "10.00", timeout: 600, purpose: "gpu", taskId: "task-99" });

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      const body = JSON.parse(init.body as string);
      expect(body.timeout).toBe(600);
      expect(body.purpose).toBe("gpu");
      expect(body.task_id).toBe("task-99");
    });

    it("validates amount is positive", async () => {
      await expect(pay.reserve({ amount: "0" })).rejects.toThrow(NexusPayError);
    });
  });

  // =========================================================================
  // commit
  // =========================================================================

  describe("commit", () => {
    it("sends POST /api/v2/pay/reserve/{id}/commit with 204", async () => {
      mockFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
      await pay.commit("res_001");

      const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("https://api.example.com/api/v2/pay/reserve/res_001/commit");
      expect(init.method).toBe("POST");
    });

    it("sends actual_amount in body when provided", async () => {
      mockFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
      await pay.commit("res_001", { actualAmount: "15.00" });

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      const body = JSON.parse(init.body as string);
      expect(body.actual_amount).toBe("15.00");
    });

    it("sends empty body when no params", async () => {
      mockFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
      await pay.commit("res_001");

      const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      const body = JSON.parse(init.body as string);
      expect(body.actual_amount).toBeUndefined();
    });

    it("handles 409 ReservationError", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "Already committed" }, 409),
      );
      await expect(pay.commit("res_001")).rejects.toThrow(ReservationError);
    });
  });

  // =========================================================================
  // release
  // =========================================================================

  describe("release", () => {
    it("sends POST /api/v2/pay/reserve/{id}/release with 204", async () => {
      mockFetch.mockResolvedValueOnce(new Response(null, { status: 204 }));
      await pay.release("res_001");

      const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("https://api.example.com/api/v2/pay/reserve/res_001/release");
      expect(init.method).toBe("POST");
    });

    it("handles 409 ReservationError", async () => {
      mockFetch.mockResolvedValueOnce(
        jsonResponse({ detail: "Already released" }, 409),
      );
      await expect(pay.release("res_001")).rejects.toThrow(ReservationError);
    });
  });

  // =========================================================================
  // meter
  // =========================================================================

  describe("meter", () => {
    it("returns MeterResult", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ success: true }));
      const result = await pay.meter({ amount: "0.01" });

      expect(result).toEqual<MeterResult>({ success: true });
    });

    it("sends POST /api/v2/pay/meter", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ success: true }));
      await pay.meter({ amount: "0.01", eventType: "api_call" });

      const [url, init] = mockFetch.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("https://api.example.com/api/v2/pay/meter");
      expect(init.method).toBe("POST");

      const body = JSON.parse(init.body as string);
      expect(body.amount).toBe("0.01");
      expect(body.event_type).toBe("api_call");
    });

    it("returns success=false for insufficient credits", async () => {
      mockFetch.mockResolvedValueOnce(jsonResponse({ success: false }));
      const result = await pay.meter({ amount: "9999.00" });
      expect(result.success).toBe(false);
    });

    it("validates amount is positive", async () => {
      await expect(pay.meter({ amount: "0" })).rejects.toThrow(NexusPayError);
    });
  });

  // =========================================================================
  // Auth propagation
  // =========================================================================

  describe("auth propagation", () => {
    it("sends Bearer token on every request", async () => {
      mockFetch
        .mockResolvedValueOnce(jsonResponse(BALANCE_API))
        .mockResolvedValueOnce(jsonResponse(BALANCE_API));

      await pay.getBalance();
      await pay.getBalance();

      for (const call of mockFetch.mock.calls) {
        const [, init] = call as [string, RequestInit];
        const headers = new Headers(init.headers);
        expect(headers.get("Authorization")).toBe("Bearer nx_test_agent1");
      }
    });
  });
});
