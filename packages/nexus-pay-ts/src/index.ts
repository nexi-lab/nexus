/**
 * @nexus-ai-fs/pay — TypeScript SDK for Nexus Pay agent payments.
 *
 * @example
 * ```typescript
 * import { NexusPay } from '@nexus-ai-fs/pay';
 *
 * const pay = new NexusPay({ apiKey: 'nx_live_myagent' });
 * const balance = await pay.getBalance();
 * console.log(`Available: ${balance.available}`);
 * ```
 *
 * @packageDocumentation
 */

// Client
export { NexusPay } from "./client.js";

// Errors (pay-specific + re-exported from @nexus-ai-fs/api-client)
export {
  NexusPayError,
  InsufficientCreditsError,
  BudgetExceededError,
  WalletNotFoundError,
  ReservationError,
  AuthenticationError,
  RateLimitError,
} from "./errors.js";

// Shared base error (for instanceof checks)
export { NexusApiError } from "@nexus-ai-fs/api-client";

// Types
export type {
  NexusPayOptions,
  RequestOptions,
  Balance,
  Receipt,
  Reservation,
  CanAffordResult,
  MeterResult,
  TransferParams,
  BatchTransferItem,
  ReserveParams,
  CommitParams,
  MeterParams,
} from "./types.js";
