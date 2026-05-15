import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { resolveConfig } from "../src/config.js";

describe("resolveConfig", () => {
  const originalEnv = { ...process.env };

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it("uses defaults when nothing is configured", () => {
    delete process.env["NEXUS_URL"];
    delete process.env["NEXUS_API_KEY"];
    const config = resolveConfig();
    expect(config.baseUrl).toBe("http://localhost:2026");
    expect(config.apiKey).toBe("");
  });

  it("reads NEXUS_URL from env", () => {
    process.env["NEXUS_URL"] = "http://remote:3000";
    const config = resolveConfig();
    expect(config.baseUrl).toBe("http://remote:3000");
  });

  it("reads NEXUS_API_KEY from env", () => {
    process.env["NEXUS_API_KEY"] = "nx_live_agent1";
    const config = resolveConfig();
    expect(config.apiKey).toBe("nx_live_agent1");
  });

  it("overrides win over env vars", () => {
    process.env["NEXUS_URL"] = "http://env:3000";
    process.env["NEXUS_API_KEY"] = "env-key";
    const config = resolveConfig({
      baseUrl: "http://override:4000",
      apiKey: "override-key",
    });
    expect(config.baseUrl).toBe("http://override:4000");
    expect(config.apiKey).toBe("override-key");
  });

  it("passes through optional fields", () => {
    const config = resolveConfig({
      apiKey: "k",
      timeout: 5000,
      maxRetries: 1,
      transformKeys: false,
    });
    expect(config.timeout).toBe(5000);
    expect(config.maxRetries).toBe(1);
    expect(config.transformKeys).toBe(false);
  });

  it("leaves optional fields undefined when not provided", () => {
    const config = resolveConfig({ apiKey: "k" });
    expect(config.timeout).toBeUndefined();
    expect(config.maxRetries).toBeUndefined();
    expect(config.fetch).toBeUndefined();
  });
});
