/**
 * End-to-end tests: TypeScript SDK → real FastAPI server with auth.
 *
 * Requires the E2E test server running:
 *   cd packages/nexus-pay-ts
 *   .venv/bin/python tests/e2e-server.py &
 *   npx vitest run tests/e2e.test.ts
 *
 * Or use the run-e2e.sh script which handles server lifecycle.
 */

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { NexusPay } from "../src/client.js";
import {
  AuthenticationError,
  NexusPayError,
} from "../src/errors.js";
import type { Balance, CanAffordResult, MeterResult, Receipt, Reservation } from "../src/types.js";

const E2E_BASE_URL = process.env["E2E_BASE_URL"] ?? "http://localhost:4219";
const E2E_API_KEY = process.env["E2E_API_KEY"] ?? "sk-e2e-test-key";

// Check if the E2E server is running before proceeding
async function isServerReady(): Promise<boolean> {
  try {
    const response = await fetch(`${E2E_BASE_URL}/health`);
    return response.ok;
  } catch {
    return false;
  }
}

describe("E2E: NexusPay TypeScript SDK → FastAPI server", () => {
  let pay: NexusPay;

  beforeAll(async () => {
    const ready = await isServerReady();
    if (!ready) {
      console.warn(
        `\n⚠ E2E test server not running at ${E2E_BASE_URL}. Skipping E2E tests.\n` +
          "  Start it with: .venv/bin/python tests/e2e-server.py\n",
      );
      return;
    }

    pay = new NexusPay({
      apiKey: E2E_API_KEY,
      baseUrl: E2E_BASE_URL,
      maxRetries: 0,
    });
  });

  // Helper to skip if server not running
  function requireServer(): void {
    if (!pay) {
      throw new Error("E2E server not running — test skipped");
    }
  }

  // =========================================================================
  // Authentication
  // =========================================================================

  describe("authentication", () => {
    it("rejects requests with invalid API key", async () => {
      const badPay = new NexusPay({
        apiKey: "sk-wrong-key-12345",
        baseUrl: E2E_BASE_URL,
        maxRetries: 0,
      });

      await expect(badPay.getBalance()).rejects.toThrow(AuthenticationError);
    });

    it("rejects requests with random bearer token", async () => {
      const badPay = new NexusPay({
        apiKey: "totally-invalid-token",
        baseUrl: E2E_BASE_URL,
        maxRetries: 0,
      });

      await expect(badPay.getBalance()).rejects.toThrow(AuthenticationError);
    });

    it("accepts requests with valid API key", async () => {
      requireServer();
      // Should not throw
      const balance = await pay.getBalance();
      expect(balance).toBeDefined();
    });
  });

  // =========================================================================
  // GET /api/v2/pay/balance
  // =========================================================================

  describe("getBalance", () => {
    it("returns balance with correct shape", async () => {
      requireServer();
      const balance = await pay.getBalance();

      expect(balance).toHaveProperty("available");
      expect(balance).toHaveProperty("reserved");
      expect(balance).toHaveProperty("total");
      expect(typeof balance.available).toBe("string");
      expect(typeof balance.reserved).toBe("string");
      expect(typeof balance.total).toBe("string");
    });

    it("returns expected mock values from E2E server", async () => {
      requireServer();
      const balance = await pay.getBalance();

      // E2E server returns available=100.000000, reserved=5.000000
      expect(balance.available).toBe("100.000000");
      expect(balance.reserved).toBe("5.000000");
      expect(balance.total).toBe("105.000000");
    });
  });

  // =========================================================================
  // GET /api/v2/pay/can-afford
  // =========================================================================

  describe("canAfford", () => {
    it("returns true for affordable amount", async () => {
      requireServer();
      const result = await pay.canAfford("50.00");

      expect(result.canAfford).toBe(true);
      expect(result.amount).toBe("50.00");
    });
  });

  // =========================================================================
  // POST /api/v2/pay/transfer
  // =========================================================================

  describe("transfer", () => {
    it("completes a credits transfer", async () => {
      requireServer();
      const receipt = await pay.transfer({
        to: "agent-bob",
        amount: "10.00",
        memo: "E2E test payment",
      });

      expect(receipt.id).toBe("tx-e2e-ts-001");
      expect(receipt.method).toBe("credits");
      expect(receipt.amount).toBe("10.00");
      expect(receipt.toAgent).toBe("agent-bob");
      expect(receipt.memo).toBe("E2E test payment");
      expect(receipt.fromAgent).toBeDefined();
    });

    it("handles idempotency key", async () => {
      requireServer();
      const receipt = await pay.transfer(
        { to: "agent-alice", amount: "5.00" },
        { idempotencyKey: "e2e-idem-001" },
      );

      expect(receipt.id).toBeDefined();
    });
  });

  // =========================================================================
  // POST /api/v2/pay/transfer/batch
  // =========================================================================

  describe("transferBatch", () => {
    it("completes a batch transfer", async () => {
      requireServer();
      const receipts = await pay.transferBatch([
        { to: "agent-a", amount: "5.00", memo: "Batch A" },
        { to: "agent-b", amount: "3.00", memo: "Batch B" },
      ]);

      expect(receipts).toHaveLength(2);
      expect(receipts[0]?.id).toBe("tx-batch-001");
      expect(receipts[1]?.id).toBe("tx-batch-002");
    });
  });

  // =========================================================================
  // POST /api/v2/pay/reserve → commit → release
  // =========================================================================

  describe("reserve/commit/release", () => {
    it("completes reserve → commit flow", async () => {
      requireServer();
      const reservation = await pay.reserve({
        amount: "25.00",
        purpose: "e2e-test",
        timeout: 600,
      });

      expect(reservation.id).toBe("res-e2e-ts-001");
      expect(reservation.amount).toBe("25.00");
      expect(reservation.purpose).toBe("e2e-test");
      expect(reservation.status).toBe("pending");

      // Commit (should return 204 → void)
      await expect(
        pay.commit(reservation.id, { actualAmount: "20.00" }),
      ).resolves.toBeUndefined();
    });

    it("completes reserve → release flow", async () => {
      requireServer();
      const reservation = await pay.reserve({ amount: "10.00" });

      // Release (should return 204 → void)
      await expect(pay.release(reservation.id)).resolves.toBeUndefined();
    });
  });

  // =========================================================================
  // POST /api/v2/pay/meter
  // =========================================================================

  describe("meter", () => {
    it("records metered usage", async () => {
      requireServer();
      const result = await pay.meter({
        amount: "0.01",
        eventType: "api_call",
      });

      expect(result.success).toBe(true);
    });
  });

  // =========================================================================
  // Full lifecycle
  // =========================================================================

  describe("full lifecycle", () => {
    it("balance → transfer → meter → reserve → commit", async () => {
      requireServer();

      // 1. Check balance
      const balance = await pay.getBalance();
      expect(parseFloat(balance.available)).toBeGreaterThan(0);

      // 2. Check affordability
      const affordable = await pay.canAfford("10.00");
      expect(affordable.canAfford).toBe(true);

      // 3. Transfer
      const receipt = await pay.transfer({
        to: "worker-agent",
        amount: "10.00",
        memo: "Lifecycle test",
      });
      expect(receipt.method).toBe("credits");

      // 4. Meter
      const meterResult = await pay.meter({ amount: "0.001" });
      expect(meterResult.success).toBe(true);

      // 5. Reserve → commit
      const reservation = await pay.reserve({ amount: "15.00", purpose: "lifecycle" });
      expect(reservation.status).toBe("pending");
      await pay.commit(reservation.id);
    });
  });
});
