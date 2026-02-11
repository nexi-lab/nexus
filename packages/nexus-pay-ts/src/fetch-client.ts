/**
 * Internal HTTP client with retry, timeout, auth, and error mapping.
 *
 * Not exported from the public API — used only by the NexusPay client class.
 */

import {
  AuthenticationError,
  BudgetExceededError,
  InsufficientCreditsError,
  NexusPayError,
  RateLimitError,
  ReservationError,
  WalletNotFoundError,
} from "./errors.js";
import type { ApiError, NexusPayOptions, RequestOptions } from "./types.js";

const DEFAULT_BASE_URL = "https://nexus.sudorouter.ai";
const DEFAULT_TIMEOUT = 30_000;
const DEFAULT_MAX_RETRIES = 3;
const INITIAL_RETRY_DELAY = 500;
const MAX_RETRY_DELAY = 8_000;
const RETRYABLE_STATUS_CODES = new Set([429, 500, 502, 503, 504]);

export class FetchClient {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly timeout: number;
  private readonly maxRetries: number;
  private readonly fetchFn: typeof globalThis.fetch;

  constructor(options: NexusPayOptions) {
    this.apiKey = options.apiKey;
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.timeout = options.timeout ?? DEFAULT_TIMEOUT;
    this.maxRetries = options.maxRetries ?? DEFAULT_MAX_RETRIES;
    this.fetchFn = options.fetch ?? globalThis.fetch;
  }

  async get<T>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>("GET", path, undefined, options);
  }

  async post<T>(path: string, body: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>("POST", path, body, options);
  }

  async postNoContent(path: string, body?: unknown, options?: RequestOptions): Promise<void> {
    await this.requestRaw("POST", path, body, options);
  }

  // ===========================================================================
  // Core request logic
  // ===========================================================================

  private async request<T>(
    method: string,
    path: string,
    body: unknown,
    options?: RequestOptions,
  ): Promise<T> {
    const response = await this.requestRaw(method, path, body, options);
    return (await response.json()) as T;
  }

  private async requestRaw(
    method: string,
    path: string,
    body: unknown,
    options?: RequestOptions,
  ): Promise<Response> {
    const url = `${this.baseUrl}${path}`;
    const headers = this.buildHeaders(method, options);
    const effectiveTimeout = options?.timeout ?? this.timeout;

    let lastError: Error | undefined;

    for (let attempt = 0; attempt <= this.maxRetries; attempt++) {
      // Wait before retry (not on first attempt)
      if (attempt > 0 && lastError) {
        const delay = this.computeRetryDelay(attempt, lastError);
        await sleep(delay);
      }

      try {
        const response = await this.executeFetch(url, method, headers, body, effectiveTimeout, options?.signal);

        if (response.ok || response.status === 204) {
          return response;
        }

        // Map HTTP status to typed error
        const error = await this.buildError(response);

        // Only retry on retryable status codes
        if (RETRYABLE_STATUS_CODES.has(response.status) && attempt < this.maxRetries) {
          lastError = error;
          continue;
        }

        throw error;
      } catch (error) {
        // If it's already a NexusPayError that isn't retryable, rethrow immediately
        if (error instanceof NexusPayError && !RETRYABLE_STATUS_CODES.has(error.status)) {
          throw error;
        }

        // Network errors (TypeError from fetch) are retryable
        if (attempt < this.maxRetries) {
          lastError = error instanceof Error ? error : new Error(String(error));
          continue;
        }

        // Retries exhausted
        if (error instanceof NexusPayError) {
          throw error;
        }
        throw new NexusPayError(
          error instanceof Error ? error.message : "Request failed",
          0,
          "network_error",
        );
      }
    }

    // Should not reach here, but just in case
    throw lastError ?? new NexusPayError("Request failed", 0, "unknown_error");
  }

  // ===========================================================================
  // Helpers
  // ===========================================================================

  private async executeFetch(
    url: string,
    method: string,
    headers: Record<string, string>,
    body: unknown,
    timeout: number,
    userSignal?: AbortSignal,
  ): Promise<Response> {
    // Fail fast if already aborted
    if (userSignal?.aborted) {
      throw new NexusPayError("Request aborted", 0, "abort_error");
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeout);
    const onUserAbort = (): void => controller.abort();

    if (userSignal) {
      userSignal.addEventListener("abort", onUserAbort, { once: true });
    }

    try {
      return await this.fetchFn(url, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        if (userSignal?.aborted) {
          throw new NexusPayError("Request aborted", 0, "abort_error");
        }
        throw new NexusPayError("Request timed out", 0, "timeout_error");
      }
      throw error;
    } finally {
      clearTimeout(timeoutId);
      if (userSignal) {
        userSignal.removeEventListener("abort", onUserAbort);
      }
    }
  }

  private buildHeaders(method: string, options?: RequestOptions): Record<string, string> {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`,
      Accept: "application/json",
    };

    if (method === "POST") {
      headers["Content-Type"] = "application/json";
    }

    if (options?.idempotencyKey) {
      headers["Idempotency-Key"] = options.idempotencyKey;
    }

    return headers;
  }

  private async buildError(response: Response): Promise<NexusPayError> {
    let message: string;
    try {
      const body = (await response.json()) as ApiError;
      message = body.detail ?? `HTTP ${response.status}`;
    } catch {
      message = `HTTP ${response.status}`;
    }

    switch (response.status) {
      case 401:
        return new AuthenticationError(message);
      case 402:
        return new InsufficientCreditsError(message);
      case 403:
        return new BudgetExceededError(message);
      case 404:
        return new WalletNotFoundError(message);
      case 409:
        return new ReservationError(message);
      case 429: {
        const retryAfterHeader = response.headers.get("Retry-After");
        const retryAfter = retryAfterHeader ? parseInt(retryAfterHeader, 10) : undefined;
        return new RateLimitError(
          message,
          Number.isNaN(retryAfter) ? undefined : retryAfter,
        );
      }
      default:
        return new NexusPayError(message, response.status, "api_error");
    }
  }

  private computeRetryDelay(attempt: number, lastError: Error): number {
    // For 429 with Retry-After, use that value (in seconds → ms)
    if (lastError instanceof RateLimitError && lastError.retryAfter !== undefined) {
      return lastError.retryAfter * 1000;
    }

    // Exponential backoff with full jitter
    const exponentialDelay = INITIAL_RETRY_DELAY * Math.pow(2, attempt - 1);
    const cappedDelay = Math.min(exponentialDelay, MAX_RETRY_DELAY);
    return Math.random() * cappedDelay;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
