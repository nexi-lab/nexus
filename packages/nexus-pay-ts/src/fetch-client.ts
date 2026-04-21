/**
 * Internal HTTP client for the Nexus Pay SDK.
 *
 * Thin wrapper around @nexus-ai-fs/api-client's FetchClient that overrides
 * error mapping to produce pay-specific error types (402, 403, 404, 409).
 *
 * Not exported from the public API — used only by the NexusPay client class.
 */

import { FetchClient as BaseFetchClient, type NexusApiError } from "@nexus-ai-fs/api-client";
import {
  BudgetExceededError,
  InsufficientCreditsError,
  NexusPayError,
  ReservationError,
  WalletNotFoundError,
} from "./errors.js";
import type { NexusPayOptions } from "./types.js";

const DEFAULT_BASE_URL = "https://nexus.sudorouter.ai";

export class FetchClient extends BaseFetchClient {
  constructor(options: NexusPayOptions) {
    super({
      apiKey: options.apiKey,
      baseUrl: options.baseUrl ?? DEFAULT_BASE_URL,
      timeout: options.timeout,
      maxRetries: options.maxRetries,
      fetch: options.fetch,
      // Disable auto key transform — NexusPay client handles mapping manually
      // to preserve backward compatibility with existing response types
      transformKeys: false,
    });
  }

  /**
   * Override error mapping to add pay-specific error types.
   *
   * 402 → InsufficientCreditsError
   * 403 → BudgetExceededError (overrides generic ForbiddenError)
   * 404 → WalletNotFoundError (overrides generic NotFoundError)
   * 409 → ReservationError (overrides generic ConflictError)
   */
  protected override async buildError(response: Response): Promise<NexusApiError> {
    // Read from a clone so super.buildError() can still parse the original body
    // for non-pay statuses (401, 429, 5xx, etc.).
    const responseForPayMapping = response.clone();
    let message: string;
    try {
      const body = (await responseForPayMapping.json()) as { detail?: string };
      message = body.detail ?? `HTTP ${response.status}`;
    } catch {
      message = `HTTP ${response.status}`;
    }

    switch (response.status) {
      case 402:
        return new InsufficientCreditsError(message);
      case 403:
        return new BudgetExceededError(message);
      case 404:
        return new WalletNotFoundError(message);
      case 409:
        return new ReservationError(message);
      default:
        // Fall through to base class for 401, 429, 5xx, etc.
        return super.buildError(response);
    }
  }
}
