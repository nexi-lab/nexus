/**
 * Core HTTP client with retry, timeout, auth, error mapping,
 * and automatic snake_case ↔ camelCase key transformation.
 *
 * Generalized from nexus-pay-ts's FetchClient. Consumer packages
 * can subclass and override `buildError()` for domain-specific error mapping.
 */

import {
  AbortError,
  AuthenticationError,
  ConflictError,
  ForbiddenError,
  NetworkError,
  NexusApiError,
  NotFoundError,
  RateLimitError,
  ServerError,
  TimeoutError,
} from "./errors.js";
import type { ApiErrorResponse, NexusClientOptions, RequestOptions } from "./types.js";
import { camelToSnakeKeys, snakeToCamelKeys } from "./case-transform.js";

const DEFAULT_BASE_URL = "http://localhost:2026";
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
  private readonly transformEnabled: boolean;
  private readonly agentId: string | undefined;
  private readonly subject: string | undefined;
  private readonly zoneId: string | undefined;

  constructor(options: NexusClientOptions) {
    this.apiKey = options.apiKey;
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, "");
    this.timeout = options.timeout ?? DEFAULT_TIMEOUT;
    this.maxRetries = options.maxRetries ?? DEFAULT_MAX_RETRIES;
    this.fetchFn = options.fetch ?? globalThis.fetch;
    this.transformEnabled = options.transformKeys ?? true;
    this.agentId = options.agentId;
    this.subject = options.subject;
    this.zoneId = options.zoneId;
  }

  async get<T>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>("GET", path, undefined, options);
  }

  async post<T>(path: string, body: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>("POST", path, body, options);
  }

  async put<T>(path: string, body: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>("PUT", path, body, options);
  }

  async patch<T>(path: string, body: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>("PATCH", path, body, options);
  }

  async delete<T>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>("DELETE", path, undefined, options);
  }

  async postNoContent(path: string, body?: unknown, options?: RequestOptions): Promise<void> {
    await this.requestRaw("POST", path, body, options);
  }

  async deleteNoContent(path: string, options?: RequestOptions): Promise<void> {
    await this.requestRaw("DELETE", path, undefined, options);
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

    if (response.status === 204) {
      return undefined as T;
    }

    const json: unknown = await response.json();
    return this.transformEnabled ? snakeToCamelKeys<T>(json) : (json as T);
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

    // Transform request body keys to snake_case
    const transformedBody =
      body !== undefined && this.transformEnabled ? camelToSnakeKeys(body) : body;

    let lastError: Error | undefined;

    for (let attempt = 0; attempt <= this.maxRetries; attempt++) {
      if (attempt > 0 && lastError) {
        const delay = this.computeRetryDelay(attempt, lastError);
        await sleep(delay);
      }

      try {
        const response = await this.executeFetch(
          url,
          method,
          headers,
          transformedBody,
          effectiveTimeout,
          options?.signal,
        );

        if (response.ok || response.status === 204) {
          return response;
        }

        const error = await this.buildError(response);

        if (RETRYABLE_STATUS_CODES.has(response.status) && attempt < this.maxRetries) {
          lastError = error;
          continue;
        }

        throw error;
      } catch (error) {
        if (error instanceof NexusApiError && !RETRYABLE_STATUS_CODES.has(error.status)) {
          throw error;
        }

        if (attempt < this.maxRetries) {
          lastError = error instanceof Error ? error : new Error(String(error));
          continue;
        }

        if (error instanceof NexusApiError) {
          throw error;
        }
        throw new NetworkError(
          error instanceof Error ? error.message : "Request failed",
        );
      }
    }

    throw lastError ?? new NetworkError("Request failed");
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
    if (userSignal?.aborted) {
      throw new AbortError("Request aborted");
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
          throw new AbortError("Request aborted");
        }
        throw new TimeoutError("Request timed out");
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

    if (method === "POST" || method === "PUT" || method === "PATCH") {
      headers["Content-Type"] = "application/json";
    }

    if (options?.idempotencyKey) {
      headers["Idempotency-Key"] = options.idempotencyKey;
    }

    // Default identity headers from client config
    if (this.agentId) headers["X-Agent-ID"] = this.agentId;
    if (this.subject) headers["X-Nexus-Subject"] = this.subject;
    if (this.zoneId) headers["X-Nexus-Zone-ID"] = this.zoneId;

    // Merge extra headers (per-request overrides take precedence)
    if (options?.headers) {
      for (const [key, value] of Object.entries(options.headers)) {
        headers[key] = value;
      }
    }

    return headers;
  }

  /**
   * Map HTTP response to a typed error.
   *
   * Subclasses can override this to add domain-specific error mapping
   * (e.g. 402 → InsufficientCreditsError in nexus-pay-ts).
   */
  protected async buildError(response: Response): Promise<NexusApiError> {
    let message: string;
    try {
      const body = (await response.json()) as ApiErrorResponse;
      message = body.detail ?? `HTTP ${response.status}`;
    } catch {
      message = `HTTP ${response.status}`;
    }

    switch (response.status) {
      case 401:
        return new AuthenticationError(message);
      case 403:
        return new ForbiddenError(message);
      case 404:
        return new NotFoundError(message);
      case 409:
        return new ConflictError(message);
      case 429: {
        const retryAfterHeader = response.headers.get("Retry-After");
        const retryAfter = retryAfterHeader ? parseInt(retryAfterHeader, 10) : undefined;
        return new RateLimitError(
          message,
          Number.isNaN(retryAfter) ? undefined : retryAfter,
        );
      }
      default:
        if (response.status >= 500) {
          return new ServerError(message, response.status);
        }
        return new NexusApiError(message, response.status, "api_error");
    }
  }

  private computeRetryDelay(attempt: number, lastError: Error): number {
    if (lastError instanceof RateLimitError && lastError.retryAfter !== undefined) {
      return lastError.retryAfter * 1000;
    }

    const exponentialDelay = INITIAL_RETRY_DELAY * Math.pow(2, attempt - 1);
    const cappedDelay = Math.min(exponentialDelay, MAX_RETRY_DELAY);
    return Math.random() * cappedDelay;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
