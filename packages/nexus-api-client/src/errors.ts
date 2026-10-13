/**
 * Base error hierarchy for all Nexus API interactions.
 *
 * HTTP-level errors live here; domain-specific errors (e.g. InsufficientCreditsError)
 * belong in consumer packages that extend NexusApiError.
 *
 * Hierarchy:
 *   NexusApiError (base)
 *   ├── AuthenticationError   (401)
 *   ├── ForbiddenError        (403)
 *   ├── NotFoundError         (404)
 *   ├── ConflictError         (409)
 *   ├── RateLimitError        (429)
 *   ├── ServerError           (5xx)
 *   ├── NetworkError          (fetch failed)
 *   ├── TimeoutError          (request timed out)
 *   └── AbortError            (user-cancelled)
 */

export class NexusApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(message: string, status: number, code: string, readonly body?: unknown) {
    super(message);
    this.name = "NexusApiError";
    this.status = status;
    this.code = code;
  }
}

export class AuthenticationError extends NexusApiError {
  constructor(message: string, body?: unknown) {
    super(message, 401, "authentication_error", body);
    this.name = "AuthenticationError";
  }
}

export class ForbiddenError extends NexusApiError {
  constructor(message: string, body?: unknown) {
    super(message, 403, "forbidden", body);
    this.name = "ForbiddenError";
  }
}

export class NotFoundError extends NexusApiError {
  constructor(message: string, body?: unknown) {
    super(message, 404, "not_found", body);
    this.name = "NotFoundError";
  }
}

export class ConflictError extends NexusApiError {
  constructor(message: string, body?: unknown) {
    super(message, 409, "conflict", body);
    this.name = "ConflictError";
  }
}

export class RateLimitError extends NexusApiError {
  readonly retryAfter: number | undefined;

  constructor(message: string, retryAfter?: number, body?: unknown) {
    super(message, 429, "rate_limit_error", body);
    this.name = "RateLimitError";
    this.retryAfter = retryAfter;
  }
}

export class ServerError extends NexusApiError {
  constructor(message: string, status: number, body?: unknown) {
    super(message, status, "server_error", body);
    this.name = "ServerError";
  }
}

/** Shared HTTP status mapping for fetch responses and worker replies. */
export function createHttpError(status: number, message: string, body?: unknown, retryAfter?: number): NexusApiError {
  switch (status) {
    case 401: return new AuthenticationError(message, body);
    case 403: return new ForbiddenError(message, body);
    case 404: return new NotFoundError(message, body);
    case 409: return new ConflictError(message, body);
    case 429: return new RateLimitError(message, retryAfter, body);
    default: return status >= 500 ? new ServerError(message, status, body)
      : new NexusApiError(message, status, "api_error", body);
  }
}

export class NetworkError extends NexusApiError {
  constructor(message: string) {
    super(message, 0, "network_error");
    this.name = "NetworkError";
  }
}

export class TimeoutError extends NexusApiError {
  constructor(message: string) {
    super(message, 0, "timeout_error");
    this.name = "TimeoutError";
  }
}

export class AbortError extends NexusApiError {
  constructor(message: string) {
    super(message, 0, "abort_error");
    this.name = "AbortError";
  }
}
