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

  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = "NexusApiError";
    this.status = status;
    this.code = code;
  }
}

export class AuthenticationError extends NexusApiError {
  constructor(message: string) {
    super(message, 401, "authentication_error");
    this.name = "AuthenticationError";
  }
}

export class ForbiddenError extends NexusApiError {
  constructor(message: string) {
    super(message, 403, "forbidden");
    this.name = "ForbiddenError";
  }
}

export class NotFoundError extends NexusApiError {
  constructor(message: string) {
    super(message, 404, "not_found");
    this.name = "NotFoundError";
  }
}

export class ConflictError extends NexusApiError {
  constructor(message: string) {
    super(message, 409, "conflict");
    this.name = "ConflictError";
  }
}

export class RateLimitError extends NexusApiError {
  readonly retryAfter: number | undefined;

  constructor(message: string, retryAfter?: number) {
    super(message, 429, "rate_limit_error");
    this.name = "RateLimitError";
    this.retryAfter = retryAfter;
  }
}

export class ServerError extends NexusApiError {
  constructor(message: string, status: number) {
    super(message, status, "server_error");
    this.name = "ServerError";
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
