/**
 * NexusPay TypeScript SDK — main client class.
 *
 * Mirrors the Python SDK's DX with idiomatic TypeScript conventions.
 * All HTTP logic is delegated to the internal FetchClient.
 *
 * @example
 * ```typescript
 * import { NexusPay } from '@nexus/pay';
 *
 * const pay = new NexusPay({ apiKey: 'sk-your-api-key' });
 * const balance = await pay.getBalance();
 * const receipt = await pay.transfer({ to: 'agent-bob', amount: '10.00', memo: 'Task' });
 * ```
 */

import { NexusPayError } from "./errors.js";
import { FetchClient } from "./fetch-client.js";
import type {
  ApiBalance,
  ApiCanAfford,
  ApiMeter,
  ApiReceipt,
  ApiReservation,
  Balance,
  BatchTransferItem,
  CanAffordResult,
  CommitParams,
  MeterParams,
  MeterResult,
  NexusPayOptions,
  Receipt,
  RequestOptions,
  ReserveParams,
  Reservation,
  TransferParams,
} from "./types.js";

/** Matches internal nx_live_<id> or nx_test_<id> format (optional). */
const NX_KEY_PATTERN = /^nx_(live|test)_(.+)$/;
const MAX_DECIMAL_PLACES = 6;
const MAX_BATCH_SIZE = 1000;

export class NexusPay {
  /**
   * Agent ID extracted from the API key (if key uses nx_live_/nx_test_ format),
   * or undefined for standard API keys (sk-*).
   */
  readonly agentId: string | undefined;

  private readonly client: FetchClient;

  constructor(options: NexusPayOptions) {
    if (!options.apiKey) {
      throw new NexusPayError("API key must not be empty", 0, "invalid_api_key");
    }

    // Extract agentId if key uses internal nx_live_/nx_test_ format
    const match = NX_KEY_PATTERN.exec(options.apiKey);
    this.agentId = match?.[2];

    this.client = new FetchClient(options);
  }

  // ===========================================================================
  // Balance operations
  // ===========================================================================

  async getBalance(options?: RequestOptions): Promise<Balance> {
    const raw = await this.client.get<ApiBalance>("/api/v2/pay/balance", options);
    return toBalance(raw);
  }

  async canAfford(amount: string, options?: RequestOptions): Promise<CanAffordResult> {
    validateAmountNonEmpty(amount);
    const raw = await this.client.get<ApiCanAfford>(
      `/api/v2/pay/can-afford?amount=${encodeURIComponent(amount)}`,
      options,
    );
    return toCanAffordResult(raw);
  }

  // ===========================================================================
  // Transfer operations
  // ===========================================================================

  async transfer(params: TransferParams, options?: RequestOptions): Promise<Receipt> {
    validateRecipient(params.to);
    validateAmount(params.amount);

    const body = {
      to: params.to,
      amount: params.amount,
      memo: params.memo ?? "",
      method: params.method ?? "auto",
      idempotency_key: options?.idempotencyKey ?? null,
    };

    const raw = await this.client.post<ApiReceipt>("/api/v2/pay/transfer", body, options);
    return toReceipt(raw);
  }

  async transferBatch(
    transfers: readonly BatchTransferItem[],
    options?: RequestOptions,
  ): Promise<Receipt[]> {
    if (transfers.length > MAX_BATCH_SIZE) {
      throw new NexusPayError(
        `Batch size ${transfers.length} exceeds maximum of ${MAX_BATCH_SIZE}`,
        0,
        "batch_too_large",
      );
    }

    const body = {
      transfers: transfers.map((t) => ({
        to: t.to,
        amount: t.amount,
        memo: t.memo ?? "",
      })),
    };

    const raw = await this.client.post<ApiReceipt[]>(
      "/api/v2/pay/transfer/batch",
      body,
      options,
    );
    return raw.map(toReceipt);
  }

  // ===========================================================================
  // Reservation operations (two-phase)
  // ===========================================================================

  async reserve(params: ReserveParams, options?: RequestOptions): Promise<Reservation> {
    validateAmount(params.amount);

    const body = {
      amount: params.amount,
      timeout: params.timeout,
      purpose: params.purpose,
      task_id: params.taskId,
    };

    const raw = await this.client.post<ApiReservation>("/api/v2/pay/reserve", body, options);
    return toReservation(raw);
  }

  async commit(
    reservationId: string,
    params?: CommitParams,
    options?: RequestOptions,
  ): Promise<void> {
    const body = {
      actual_amount: params?.actualAmount,
    };

    await this.client.postNoContent(
      `/api/v2/pay/reserve/${encodeURIComponent(reservationId)}/commit`,
      body,
      options,
    );
  }

  async release(reservationId: string, options?: RequestOptions): Promise<void> {
    await this.client.postNoContent(
      `/api/v2/pay/reserve/${encodeURIComponent(reservationId)}/release`,
      undefined,
      options,
    );
  }

  // ===========================================================================
  // Metering
  // ===========================================================================

  async meter(params: MeterParams, options?: RequestOptions): Promise<MeterResult> {
    validateAmount(params.amount);

    const body = {
      amount: params.amount,
      event_type: params.eventType ?? "api_call",
    };

    const raw = await this.client.post<ApiMeter>("/api/v2/pay/meter", body, options);
    return toMeterResult(raw);
  }
}

// =============================================================================
// Validation helpers (pure functions, no mutation)
// =============================================================================

/** Regex: positive decimal number (digits, optional dot + digits, no leading minus). */
const AMOUNT_PATTERN = /^\d+(\.\d+)?$/;
/** Regex: zero in all forms — "0", "0.0", "0.000000". */
const ZERO_PATTERN = /^0+(\.0+)?$/;

function validateAmount(amount: string): void {
  if (!amount) {
    throw new NexusPayError("Amount must not be empty", 0, "validation_error");
  }

  if (!AMOUNT_PATTERN.test(amount)) {
    throw new NexusPayError(`Invalid amount: '${amount}'`, 0, "validation_error");
  }

  if (ZERO_PATTERN.test(amount)) {
    throw new NexusPayError("Amount must be positive", 0, "validation_error");
  }

  // Check decimal places
  const dotIndex = amount.indexOf(".");
  if (dotIndex !== -1) {
    const decimals = amount.length - dotIndex - 1;
    if (decimals > MAX_DECIMAL_PLACES) {
      throw new NexusPayError(
        `Amount must have at most ${MAX_DECIMAL_PLACES} decimal places`,
        0,
        "validation_error",
      );
    }
  }
}

function validateAmountNonEmpty(amount: string): void {
  if (!amount) {
    throw new NexusPayError("Amount must not be empty", 0, "validation_error");
  }
}

function validateRecipient(to: string): void {
  if (!to) {
    throw new NexusPayError("Recipient 'to' must not be empty", 0, "validation_error");
  }
}

// =============================================================================
// Response mappers (snake_case API → camelCase SDK, immutable)
// =============================================================================

function toBalance(raw: ApiBalance): Balance {
  return {
    available: raw.available,
    reserved: raw.reserved,
    total: raw.total,
  };
}

function toReceipt(raw: ApiReceipt): Receipt {
  return {
    id: raw.id,
    method: raw.method,
    amount: raw.amount,
    fromAgent: raw.from_agent,
    toAgent: raw.to_agent,
    memo: raw.memo,
    timestamp: raw.timestamp,
    txHash: raw.tx_hash,
  };
}

function toReservation(raw: ApiReservation): Reservation {
  return {
    id: raw.id,
    amount: raw.amount,
    purpose: raw.purpose,
    expiresAt: raw.expires_at,
    status: raw.status,
  };
}

function toCanAffordResult(raw: ApiCanAfford): CanAffordResult {
  return {
    canAfford: raw.can_afford,
    amount: raw.amount,
  };
}

function toMeterResult(raw: ApiMeter): MeterResult {
  return {
    success: raw.success,
  };
}
