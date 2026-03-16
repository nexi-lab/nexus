import { describe, it, expect } from "vitest";
import {
  NexusApiError,
  AuthenticationError,
  ForbiddenError,
  NotFoundError,
  ConflictError,
  RateLimitError,
  ServerError,
  NetworkError,
  TimeoutError,
  AbortError,
} from "../src/errors.js";

describe("NexusApiError", () => {
  it("stores status and code", () => {
    const err = new NexusApiError("test", 418, "teapot");
    expect(err.message).toBe("test");
    expect(err.status).toBe(418);
    expect(err.code).toBe("teapot");
    expect(err.name).toBe("NexusApiError");
    expect(err).toBeInstanceOf(Error);
  });
});

describe("AuthenticationError", () => {
  it("has status 401", () => {
    const err = new AuthenticationError("bad key");
    expect(err.status).toBe(401);
    expect(err.code).toBe("authentication_error");
    expect(err.name).toBe("AuthenticationError");
    expect(err).toBeInstanceOf(NexusApiError);
    expect(err).toBeInstanceOf(Error);
  });
});

describe("ForbiddenError", () => {
  it("has status 403", () => {
    const err = new ForbiddenError("no access");
    expect(err.status).toBe(403);
    expect(err.code).toBe("forbidden");
    expect(err).toBeInstanceOf(NexusApiError);
  });
});

describe("NotFoundError", () => {
  it("has status 404", () => {
    const err = new NotFoundError("missing");
    expect(err.status).toBe(404);
    expect(err.code).toBe("not_found");
    expect(err).toBeInstanceOf(NexusApiError);
  });
});

describe("ConflictError", () => {
  it("has status 409", () => {
    const err = new ConflictError("conflict");
    expect(err.status).toBe(409);
    expect(err.code).toBe("conflict");
    expect(err).toBeInstanceOf(NexusApiError);
  });
});

describe("RateLimitError", () => {
  it("has status 429 and optional retryAfter", () => {
    const err = new RateLimitError("slow down", 30);
    expect(err.status).toBe(429);
    expect(err.retryAfter).toBe(30);
    expect(err).toBeInstanceOf(NexusApiError);
  });

  it("retryAfter is undefined when not provided", () => {
    const err = new RateLimitError("slow down");
    expect(err.retryAfter).toBeUndefined();
  });
});

describe("ServerError", () => {
  it("stores actual status code", () => {
    const err = new ServerError("internal", 502);
    expect(err.status).toBe(502);
    expect(err.code).toBe("server_error");
    expect(err).toBeInstanceOf(NexusApiError);
  });
});

describe("NetworkError", () => {
  it("has status 0", () => {
    const err = new NetworkError("offline");
    expect(err.status).toBe(0);
    expect(err.code).toBe("network_error");
    expect(err).toBeInstanceOf(NexusApiError);
  });
});

describe("TimeoutError", () => {
  it("has status 0", () => {
    const err = new TimeoutError("timed out");
    expect(err.status).toBe(0);
    expect(err.code).toBe("timeout_error");
    expect(err).toBeInstanceOf(NexusApiError);
  });
});

describe("AbortError", () => {
  it("has status 0", () => {
    const err = new AbortError("cancelled");
    expect(err.status).toBe(0);
    expect(err.code).toBe("abort_error");
    expect(err).toBeInstanceOf(NexusApiError);
  });
});
