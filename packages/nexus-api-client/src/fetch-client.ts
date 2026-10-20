/**
 * Core HTTP client with retry, timeout, auth, error mapping,
 * and automatic snake_case ↔ camelCase key transformation.
 */

import {
  AbortError,
  createHttpError,
  NetworkError,
  NexusApiError,
  RateLimitError,
  TimeoutError,
} from "./errors.js";
import type {
  NexusClientOptions,
  RequestOptions,
} from "./types.js";
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

  /**
   * Execute an arbitrary HTTP request through the authenticated client.
   *
   * Returns the raw `Response` — no JSON parsing, no key transformation,
   * no retries. Auth and identity headers are injected automatically.
   * Timeout is enforced (defaults to client timeout, overridable per-request).
   *
   * Body is sent as-is. The response body is buffered before returning so
   * cancellation and timeout cover the full transfer, including file bytes.
   *
   * Intended for the API Console and similar exploratory tools that need
   * full control over the request/response while still using real auth.
   */
  async rawRequest(
    method: string,
    path: string,
    body?: string | Uint8Array,
    options?: RequestOptions,
  ): Promise<Response> {
    const url = this.resolveUrl(path, options);
    const headers = this.buildHeaders(method, options);
    const effectiveTimeout = options?.timeout ?? this.timeout;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), effectiveTimeout);
    const userSignal = options?.signal;
    const onUserAbort = (): void => controller.abort();

    if (userSignal) {
      if (userSignal.aborted) {
        clearTimeout(timeoutId);
        throw new AbortError("Request aborted");
      }
      userSignal.addEventListener("abort", onUserAbort, { once: true });
    }

    try {
      const response = await this.fetchFn(url, {
        method,
        headers,
        body: body instanceof Uint8Array ? new Uint8Array(body) : body,
        signal: controller.signal,
      });
      const bytes = await response.arrayBuffer();
      return new Response([204, 205, 304].includes(response.status) ? null : bytes, {
        status: response.status, statusText: response.statusText, headers: response.headers,
      });
    } catch (error) {
      if (isAbortError(error)) {
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
    return (options?.transformKeys ?? this.transformEnabled) ? snakeToCamelKeys<T>(json) : (json as T);
  }

  private async requestRaw(
    method: string,
    path: string,
    body: unknown,
    options?: RequestOptions,
  ): Promise<Response> {
    const normalizedMethod = method.toUpperCase();
    const url = this.resolveUrl(path, options);
    const headers = this.buildHeaders(method, options);
    const effectiveTimeout = options?.timeout ?? this.timeout;
    const retryableRequest = this.isRetryableRequest(normalizedMethod, options);

    // Transform request body keys to snake_case
    const transformedBody =
      body !== undefined && (options?.transformKeys ?? this.transformEnabled) ? camelToSnakeKeys(body) : body;

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

        if (
          retryableRequest &&
          RETRYABLE_STATUS_CODES.has(response.status) &&
          attempt < this.maxRetries
        ) {
          lastError = error;
          continue;
        }

        throw error;
      } catch (error) {
        if (
          error instanceof NexusApiError &&
          (!retryableRequest || !RETRYABLE_STATUS_CODES.has(error.status))
        ) {
          throw error;
        }

        if (retryableRequest && attempt < this.maxRetries) {
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
      if (isAbortError(error)) {
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

  private resolveUrl(path: string, options?: RequestOptions): string {
    const base = options?.baseUrl?.replace(/\/+$/, "") ?? this.baseUrl;
    const url = `${base}${path}`;
    if (!options?.query) return url;
    const destination = new URL(url);
    for (const [name, value] of Object.entries(options.query)) {
      if (value !== undefined) destination.searchParams.set(name, String(value));
    }
    return destination.toString();
  }

  /**
   * Map HTTP response to a typed error.
   */
  protected async buildError(response: Response): Promise<NexusApiError> {
    let message = `HTTP ${response.status}`;
    let body: unknown;
    try {
      body = await response.json();
      if (body && typeof body === "object") {
        const fields = body as Record<string, unknown>;
        const detail = fields["detail"];
        const nested = detail && typeof detail === "object" ? detail as Record<string, unknown> : {};
        const text = [detail, fields["error"], fields["reason"], nested["error"], nested["reason"]]
          .find(value => typeof value === "string");
        if (typeof text === "string") message = text;
      }
    } catch {
      message = `HTTP ${response.status}`;
    }

    const retryAfterHeader = response.headers.get("Retry-After");
    const retryAfter = retryAfterHeader ? parseInt(retryAfterHeader, 10) : undefined;
    return createHttpError(response.status, message, body,
      Number.isNaN(retryAfter) ? undefined : retryAfter);
  }

  private computeRetryDelay(attempt: number, lastError: Error): number {
    if (lastError instanceof RateLimitError && lastError.retryAfter !== undefined) {
      return lastError.retryAfter * 1000;
    }

    const exponentialDelay = INITIAL_RETRY_DELAY * Math.pow(2, attempt - 1);
    const cappedDelay = Math.min(exponentialDelay, MAX_RETRY_DELAY);
    return Math.random() * cappedDelay;
  }

  private isRetryableRequest(
    method: string,
    options?: RequestOptions,
  ): boolean {
    switch (method) {
      case "GET":
      case "HEAD":
      case "OPTIONS":
      case "TRACE":
      case "PUT":
      case "DELETE":
        return true;
      default:
        return Boolean(options?.idempotencyKey);
    }
  }


}

function isAbortError(error: unknown): boolean {
  if (typeof DOMException !== "undefined" && error instanceof DOMException) {
    return error.name === "AbortError";
  }
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
