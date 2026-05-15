/**
 * Error classes for the Nexus Pay SDK.
 *
 * Extends the shared NexusApiError base from @nexus-ai-fs/api-client
 * with payment-specific error types.
 *
 * Hierarchy:
 *   NexusApiError (from @nexus-ai-fs/api-client)
 *   └── NexusPayError (base for pay-specific errors)
 *       ├── InsufficientCreditsError (402)
 *       ├── BudgetExceededError      (403)
 *       ├── WalletNotFoundError      (404)
 *       └── ReservationError         (409)
 *
 * Note: AuthenticationError (401) and RateLimitError (429) are
 * re-exported from @nexus-ai-fs/api-client — no pay-specific override needed.
 */

import { NexusApiError } from "@nexus-ai-fs/api-client";

export class NexusPayError extends NexusApiError {
  constructor(message: string, status: number, code: string) {
    super(message, status, code);
    this.name = "NexusPayError";
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

// Re-export shared errors for backward compatibility
export { AuthenticationError, RateLimitError } from "@nexus-ai-fs/api-client";
