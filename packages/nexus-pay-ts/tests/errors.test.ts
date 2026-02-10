import { describe, expect, it } from "vitest";
import {
  NexusPayError,
  AuthenticationError,
  InsufficientCreditsError,
  BudgetExceededError,
  WalletNotFoundError,
  ReservationError,
  RateLimitError,
} from "../src/errors.js";

describe("NexusPayError", () => {
  it("has correct name, message, status, and code", () => {
    const error = new NexusPayError("something broke", 400, "pay_error");
    expect(error.name).toBe("NexusPayError");
    expect(error.message).toBe("something broke");
    expect(error.status).toBe(400);
    expect(error.code).toBe("pay_error");
  });

  it("is an instance of Error", () => {
    const error = new NexusPayError("test", 400, "pay_error");
    expect(error).toBeInstanceOf(Error);
    expect(error).toBeInstanceOf(NexusPayError);
  });

  it("has a stack trace", () => {
    const error = new NexusPayError("test", 400, "pay_error");
    expect(error.stack).toBeDefined();
  });
});

describe("AuthenticationError", () => {
  it("has status 401 and correct code", () => {
    const error = new AuthenticationError("invalid key");
    expect(error.status).toBe(401);
    expect(error.code).toBe("authentication_error");
    expect(error.name).toBe("AuthenticationError");
  });

  it("is an instance of NexusPayError", () => {
    const error = new AuthenticationError("invalid key");
    expect(error).toBeInstanceOf(NexusPayError);
    expect(error).toBeInstanceOf(Error);
  });
});

describe("InsufficientCreditsError", () => {
  it("has status 402 and correct code", () => {
    const error = new InsufficientCreditsError("not enough credits");
    expect(error.status).toBe(402);
    expect(error.code).toBe("insufficient_credits");
    expect(error.name).toBe("InsufficientCreditsError");
  });

  it("is an instance of NexusPayError", () => {
    const error = new InsufficientCreditsError("test");
    expect(error).toBeInstanceOf(NexusPayError);
  });
});

describe("BudgetExceededError", () => {
  it("has status 403 and correct code", () => {
    const error = new BudgetExceededError("budget limit");
    expect(error.status).toBe(403);
    expect(error.code).toBe("budget_exceeded");
    expect(error.name).toBe("BudgetExceededError");
  });

  it("is an instance of NexusPayError", () => {
    const error = new BudgetExceededError("test");
    expect(error).toBeInstanceOf(NexusPayError);
  });
});

describe("WalletNotFoundError", () => {
  it("has status 404 and correct code", () => {
    const error = new WalletNotFoundError("wallet missing");
    expect(error.status).toBe(404);
    expect(error.code).toBe("wallet_not_found");
    expect(error.name).toBe("WalletNotFoundError");
  });

  it("is an instance of NexusPayError", () => {
    const error = new WalletNotFoundError("test");
    expect(error).toBeInstanceOf(NexusPayError);
  });
});

describe("ReservationError", () => {
  it("has status 409 and correct code", () => {
    const error = new ReservationError("reservation conflict");
    expect(error.status).toBe(409);
    expect(error.code).toBe("reservation_error");
    expect(error.name).toBe("ReservationError");
  });

  it("is an instance of NexusPayError", () => {
    const error = new ReservationError("test");
    expect(error).toBeInstanceOf(NexusPayError);
  });
});

describe("RateLimitError", () => {
  it("has status 429 and correct code", () => {
    const error = new RateLimitError("rate limited", 30);
    expect(error.status).toBe(429);
    expect(error.code).toBe("rate_limit_error");
    expect(error.name).toBe("RateLimitError");
  });

  it("exposes retryAfter property", () => {
    const error = new RateLimitError("slow down", 60);
    expect(error.retryAfter).toBe(60);
  });

  it("handles undefined retryAfter", () => {
    const error = new RateLimitError("slow down");
    expect(error.retryAfter).toBeUndefined();
  });

  it("is an instance of NexusPayError", () => {
    const error = new RateLimitError("test");
    expect(error).toBeInstanceOf(NexusPayError);
  });
});
