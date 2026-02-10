/**
 * Error classes for the Nexus Pay SDK.
 *
 * Hierarchy mirrors the Python SDK's exception classes:
 *   NexusPayError (base)
 *   ├── AuthenticationError      (401)
 *   ├── InsufficientCreditsError (402)
 *   ├── BudgetExceededError      (403)
 *   ├── WalletNotFoundError      (404)
 *   ├── ReservationError         (409)
 *   └── RateLimitError           (429)
 */

export class NexusPayError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = "NexusPayError";
    this.status = status;
    this.code = code;
  }
}

export class AuthenticationError extends NexusPayError {
  constructor(message: string) {
    super(message, 401, "authentication_error");
    this.name = "AuthenticationError";
  }
}

export class InsufficientCreditsError extends NexusPayError {
  constructor(message: string) {
    super(message, 402, "insufficient_credits");
    this.name = "InsufficientCreditsError";
  }
}

export class BudgetExceededError extends NexusPayError {
  constructor(message: string) {
    super(message, 403, "budget_exceeded");
    this.name = "BudgetExceededError";
  }
}

export class WalletNotFoundError extends NexusPayError {
  constructor(message: string) {
    super(message, 404, "wallet_not_found");
    this.name = "WalletNotFoundError";
  }
}

export class ReservationError extends NexusPayError {
  constructor(message: string) {
    super(message, 409, "reservation_error");
    this.name = "ReservationError";
  }
}

export class RateLimitError extends NexusPayError {
  readonly retryAfter: number | undefined;

  constructor(message: string, retryAfter?: number) {
    super(message, 429, "rate_limit_error");
    this.name = "RateLimitError";
    this.retryAfter = retryAfter;
  }
}
