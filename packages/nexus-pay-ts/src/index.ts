/**
 * @nexus/pay — TypeScript SDK for Nexus Pay agent payments.
 *
 * @example
 * ```typescript
 * import { NexusPay } from '@nexus/pay';
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

// Errors
export {
  NexusPayError,
  AuthenticationError,
  InsufficientCreditsError,
  BudgetExceededError,
  WalletNotFoundError,
  ReservationError,
  RateLimitError,
} from "./errors.js";

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
