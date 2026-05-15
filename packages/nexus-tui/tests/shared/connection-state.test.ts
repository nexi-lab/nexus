/**
 * Tests for detectConnectionState() — Issue #12A.
 *
 * Pure function tests following the detectFreshServer pattern.
 */

import { describe, it, expect } from "bun:test";
import { detectConnectionState } from "../../src/shared/hooks/use-connection-state.js";
import type { NexusClientOptions } from "@nexus-ai-fs/api-client";

const baseConfig: NexusClientOptions = {
  baseUrl: "http://localhost:2026",
  apiKey: "sk-test-key",
};

const noKeyConfig: NexusClientOptions = {
  baseUrl: "http://localhost:2026",
  apiKey: "",
};

describe("detectConnectionState", () => {
  // =========================================================================
  // Happy path
  // =========================================================================
  it("returns 'ready' when connected", () => {
    expect(detectConnectionState("connected", null, baseConfig)).toBe("ready");
  });

  // =========================================================================
  // No config
  // =========================================================================
  it("returns 'no-config' when no API key (empty string)", () => {
    expect(detectConnectionState("disconnected", null, noKeyConfig)).toBe("no-config");
  });

  it("returns 'no-config' when no API key (undefined)", () => {
    expect(detectConnectionState("disconnected", null, {
      baseUrl: "http://localhost:2026",
    } as NexusClientOptions)).toBe("no-config");
  });

  it("returns 'no-config' even when error state but no key", () => {
    expect(detectConnectionState("error", "some error", noKeyConfig)).toBe("no-config");
  });

  // =========================================================================
  // Connecting
  // =========================================================================
  it("returns 'connecting' when status is connecting", () => {
    expect(detectConnectionState("connecting", null, baseConfig)).toBe("connecting");
  });

  it("returns 'connecting' when status is disconnected (initial state)", () => {
    expect(detectConnectionState("disconnected", null, baseConfig)).toBe("connecting");
  });

  // =========================================================================
  // Server unreachable
  // =========================================================================
  it("returns 'no-server' for ECONNREFUSED", () => {
    expect(detectConnectionState("error", "ECONNREFUSED", baseConfig)).toBe("no-server");
  });

  it("returns 'no-server' for timeout", () => {
    expect(detectConnectionState("error", "Request timed out", baseConfig)).toBe("no-server");
  });

  it("returns 'no-server' for generic network error", () => {
    expect(detectConnectionState("error", "Network error", baseConfig)).toBe("no-server");
  });

  it("returns 'no-server' for null error message", () => {
    expect(detectConnectionState("error", null, baseConfig)).toBe("no-server");
  });

  it("returns 'no-server' for 500 server error", () => {
    expect(detectConnectionState("error", "Internal Server Error (500)", baseConfig)).toBe("no-server");
  });

  // =========================================================================
  // Auth failures
  // =========================================================================
  it("returns 'auth-failed' for 401", () => {
    expect(detectConnectionState("error", "Unauthorized (401)", baseConfig)).toBe("auth-failed");
  });

  it("returns 'auth-failed' for 403", () => {
    expect(detectConnectionState("error", "Forbidden (403)", baseConfig)).toBe("auth-failed");
  });

  it("returns 'auth-failed' for lowercase unauthorized", () => {
    expect(detectConnectionState("error", "request unauthorized", baseConfig)).toBe("auth-failed");
  });

  it("returns 'auth-failed' for error containing 401", () => {
    expect(detectConnectionState("error", "HTTP 401: Invalid API key", baseConfig)).toBe("auth-failed");
  });

  it("returns 'auth-failed' for error containing 403", () => {
    expect(detectConnectionState("error", "HTTP 403: Access denied", baseConfig)).toBe("auth-failed");
  });
});
