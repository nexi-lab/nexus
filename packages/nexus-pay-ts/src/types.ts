/**
 * Type definitions for the Nexus Pay SDK.
 *
 * All monetary amounts are `string` to prevent floating-point precision loss.
 * This matches the REST API wire format (JSON string amounts).
 */

import type { NexusClientOptions, RequestOptions as BaseRequestOptions } from "@nexus-ai-fs/api-client";

// =============================================================================
// SDK Configuration (extends shared client options)
// =============================================================================

/**
 * Options for creating a NexusPay client.
 * Extends NexusClientOptions from @nexus-ai-fs/api-client.
 */
export type NexusPayOptions = NexusClientOptions;

/**
 * Per-request options.
 * Re-exported from @nexus-ai-fs/api-client for backward compatibility.
 */
export type RequestOptions = BaseRequestOptions;

// =============================================================================
// Response Types (match REST API Pydantic models in pay.py)
// =============================================================================

export interface Balance {
  readonly available: string;
  readonly reserved: string;
  readonly total: string;
}

export interface Receipt {
  readonly id: string;
  readonly method: string;
  readonly amount: string;
  readonly fromAgent: string;
  readonly toAgent: string;
  readonly memo: string | null;
  readonly timestamp: string | null;
  readonly txHash: string | null;
}

export interface Reservation {
  readonly id: string;
  readonly amount: string;
  readonly purpose: string;
  readonly expiresAt: string | null;
  readonly status: string;
}

export interface CanAffordResult {
  readonly canAfford: boolean;
  readonly amount: string;
}

export interface MeterResult {
  readonly success: boolean;
}

// =============================================================================
// Request Types (match REST API Pydantic models in pay.py)
// =============================================================================

export interface TransferParams {
  /** Recipient agent ID or wallet address. */
  readonly to: string;

  /** Amount as decimal string (e.g. "10.50"). */
  readonly amount: string;

  /** Optional memo/description. */
  readonly memo?: string;

  /** Payment method: "auto" (default), "credits", or "x402". */
  readonly method?: "auto" | "credits" | "x402";
}

export interface BatchTransferItem {
  /** Recipient agent ID. */
  readonly to: string;

  /** Amount as decimal string. */
  readonly amount: string;

  /** Optional memo. */
  readonly memo?: string;
}

export interface ReserveParams {
  /** Amount to reserve as decimal string. */
  readonly amount: string;

  /** Auto-release timeout in seconds (1-86400). Default: 300. */
  readonly timeout?: number;

  /** Purpose of reservation. Default: "general". */
  readonly purpose?: string;

  /** Optional task identifier. */
  readonly taskId?: string;
}

export interface CommitParams {
  /** Actual amount to charge (omit for full reserved amount). */
  readonly actualAmount?: string;
}

export interface MeterParams {
  /** Amount to deduct as decimal string. */
  readonly amount: string;

  /** Type of metered event. Default: "api_call". */
  readonly eventType?: string;
}

// =============================================================================
// Internal API wire types (snake_case, matching JSON responses)
// =============================================================================

/** @internal */
export interface ApiBalance {
  readonly available: string;
  readonly reserved: string;
  readonly total: string;
}

/** @internal */
export interface ApiReceipt {
  readonly id: string;
  readonly method: string;
  readonly amount: string;
  readonly from_agent: string;
  readonly to_agent: string;
  readonly memo: string | null;
  readonly timestamp: string | null;
  readonly tx_hash: string | null;
}

/** @internal */
export interface ApiReservation {
  readonly id: string;
  readonly amount: string;
  readonly purpose: string;
  readonly expires_at: string | null;
  readonly status: string;
}

/** @internal */
export interface ApiCanAfford {
  readonly can_afford: boolean;
  readonly amount: string;
}

/** @internal */
export interface ApiMeter {
  readonly success: boolean;
}

/** @internal */
export interface ApiError {
  readonly detail: string;
  readonly error_code?: string;
}
