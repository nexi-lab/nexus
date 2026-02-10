/**
 * Contract tests: validate that SDK types align with the REST API's
 * Pydantic model field names and structure.
 *
 * These tests don't hit a live server — they verify the mapping between
 * snake_case API responses and camelCase SDK types is correct by testing
 * with realistic payloads that match the Pydantic model structure.
 */

import { describe, expect, it, vi } from "vitest";
import { NexusPay } from "../src/client.js";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makePay(mockFetch: ReturnType<typeof vi.fn>): NexusPay {
  return new NexusPay({
    apiKey: "nx_test_contract",
    baseUrl: "https://api.test.com",
    maxRetries: 0,
    fetch: mockFetch,
  });
}

describe("contract: BalanceResponse", () => {
  it("maps API snake_case fields to SDK camelCase", async () => {
    // Matches pay.py BalanceResponse model exactly
    const apiPayload = {
      available: "123.456000",
      reserved: "10.000000",
      total: "133.456000",
    };

    const mockFetch = vi.fn().mockResolvedValueOnce(jsonResponse(apiPayload));
    const pay = makePay(mockFetch);
    const balance = await pay.getBalance();

    expect(balance).toHaveProperty("available");
    expect(balance).toHaveProperty("reserved");
    expect(balance).toHaveProperty("total");
    // No snake_case keys should leak through
    expect(balance).not.toHaveProperty("available_balance");
  });
});

describe("contract: ReceiptResponse", () => {
  it("maps API snake_case fields to SDK camelCase", async () => {
    // Matches pay.py ReceiptResponse model exactly
    const apiPayload = {
      id: "tx_12345",
      method: "credits",
      amount: "10.000000",
      from_agent: "sender-agent",
      to_agent: "receiver-agent",
      memo: "Payment for task",
      timestamp: "2025-06-01T12:00:00Z",
      tx_hash: null,
    };

    const mockFetch = vi.fn().mockResolvedValueOnce(jsonResponse(apiPayload, 201));
    const pay = makePay(mockFetch);
    const receipt = await pay.transfer({ to: "receiver-agent", amount: "10.00" });

    // Verify camelCase mapping
    expect(receipt.fromAgent).toBe("sender-agent");
    expect(receipt.toAgent).toBe("receiver-agent");
    expect(receipt.txHash).toBeNull();
    // Verify no snake_case leaks
    expect(receipt).not.toHaveProperty("from_agent");
    expect(receipt).not.toHaveProperty("to_agent");
    expect(receipt).not.toHaveProperty("tx_hash");
  });
});

describe("contract: ReservationResponse", () => {
  it("maps API snake_case fields to SDK camelCase", async () => {
    // Matches pay.py ReservationResponse model exactly
    const apiPayload = {
      id: "res_99",
      amount: "50.000000",
      purpose: "gpu-compute",
      expires_at: "2025-06-01T13:00:00Z",
      status: "pending",
    };

    const mockFetch = vi.fn().mockResolvedValueOnce(jsonResponse(apiPayload, 201));
    const pay = makePay(mockFetch);
    const reservation = await pay.reserve({ amount: "50.00" });

    expect(reservation.expiresAt).toBe("2025-06-01T13:00:00Z");
    expect(reservation).not.toHaveProperty("expires_at");
  });
});

describe("contract: CanAffordResponse", () => {
  it("maps API snake_case fields to SDK camelCase", async () => {
    // Matches pay.py CanAffordResponse model exactly
    const apiPayload = {
      can_afford: true,
      amount: "25.000000",
    };

    const mockFetch = vi.fn().mockResolvedValueOnce(jsonResponse(apiPayload));
    const pay = makePay(mockFetch);
    const result = await pay.canAfford("25.00");

    expect(result.canAfford).toBe(true);
    expect(result).not.toHaveProperty("can_afford");
  });
});

describe("contract: MeterResponse", () => {
  it("maps API fields correctly (no snake_case conversion needed)", async () => {
    // Matches pay.py MeterResponse model exactly
    const apiPayload = {
      success: true,
    };

    const mockFetch = vi.fn().mockResolvedValueOnce(jsonResponse(apiPayload));
    const pay = makePay(mockFetch);
    const result = await pay.meter({ amount: "0.01" });

    expect(result.success).toBe(true);
  });
});

describe("contract: request body field names", () => {
  it("transfer sends snake_case field names matching API", async () => {
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          { id: "x", method: "credits", amount: "1", from_agent: "a", to_agent: "b", memo: null, timestamp: null, tx_hash: null },
          201,
        ),
      );
    const pay = makePay(mockFetch);
    await pay.transfer(
      { to: "bob", amount: "1.00", method: "credits" },
      { idempotencyKey: "key-1" },
    );

    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    // API expects snake_case
    expect(body).toHaveProperty("idempotency_key");
    expect(body).not.toHaveProperty("idempotencyKey");
  });

  it("reserve sends snake_case field names matching API", async () => {
    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ id: "r1", amount: "10", purpose: "x", expires_at: null, status: "pending" }, 201),
      );
    const pay = makePay(mockFetch);
    await pay.reserve({ amount: "10.00", taskId: "task-1" });

    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body).toHaveProperty("task_id");
    expect(body).not.toHaveProperty("taskId");
  });

  it("commit sends snake_case field names matching API", async () => {
    const mockFetch = vi.fn().mockResolvedValueOnce(new Response(null, { status: 204 }));
    const pay = makePay(mockFetch);
    await pay.commit("res_1", { actualAmount: "5.00" });

    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body).toHaveProperty("actual_amount");
    expect(body).not.toHaveProperty("actualAmount");
  });

  it("meter sends snake_case field names matching API", async () => {
    const mockFetch = vi.fn().mockResolvedValueOnce(jsonResponse({ success: true }));
    const pay = makePay(mockFetch);
    await pay.meter({ amount: "0.01", eventType: "llm_call" });

    const [, init] = mockFetch.mock.calls[0] as [string, RequestInit];
    const body = JSON.parse(init.body as string);
    expect(body).toHaveProperty("event_type");
    expect(body).not.toHaveProperty("eventType");
  });
});
