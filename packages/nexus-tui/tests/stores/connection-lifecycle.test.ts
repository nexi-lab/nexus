/**
 * Tests for connection lifecycle transitions — Phase 0, Issue #10A.
 *
 * Tests the full initConfig → testConnection flow with mocked FetchClient,
 * covering happy path + all error modes. These tests validate the signals
 * that the PreConnectionScreen will rely on.
 */

import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useGlobalStore } from "../../src/stores/global-store.js";
import type { FetchClient } from "@nexus/api-client";

function createMockClient(overrides: {
  health?: () => Promise<unknown>;
  features?: () => Promise<unknown>;
  authMe?: () => Promise<unknown>;
} = {}): FetchClient {
  const defaultUserInfo = {
    user_id: "user-1",
    email: "test@example.com",
    username: "testuser",
    display_name: "Test User",
    avatar_url: null,
    is_global_admin: false,
    primary_auth_method: "api_key",
  };

  const defaultHealth = { status: "ready", uptime_seconds: 100 };
  const defaultFeatures = {
    profile: "full",
    mode: "standalone",
    enabled_bricks: ["search", "catalog"],
    disabled_bricks: [],
    version: "0.9.0",
    rate_limit_enabled: false,
  };

  return {
    get: mock(async (url: string) => {
      if (url === "/auth/me") {
        return overrides.authMe ? overrides.authMe() : defaultUserInfo;
      }
      if (url === "/healthz/ready") {
        return overrides.health ? overrides.health() : defaultHealth;
      }
      if (url === "/api/v2/features") {
        return overrides.features ? overrides.features() : defaultFeatures;
      }
      throw new Error(`Unmocked URL: ${url}`);
    }),
    rawRequest: mock(async () => new Response("{}", { status: 200 })),
  } as unknown as FetchClient;
}

describe("Connection Lifecycle", () => {
  beforeEach(() => {
    useGlobalStore.setState({
      connectionStatus: "disconnected",
      connectionError: null,
      client: null,
      serverVersion: null,
      zoneId: null,
      uptime: null,
      userInfo: null,
      enabledBricks: [],
      profile: null,
      mode: null,
      featuresLoaded: false,
      featuresLastFetched: 0,
    });
  });

  describe("happy path: disconnected → connecting → connected", () => {
    it("testConnection sets connected + userInfo on success", async () => {
      const client = createMockClient();
      useGlobalStore.setState({ client });

      await useGlobalStore.getState().testConnection();

      const state = useGlobalStore.getState();
      expect(state.connectionStatus).toBe("connected");
      expect(state.connectionError).toBeNull();
      expect(state.userInfo).not.toBeNull();
      expect(state.userInfo!.email).toBe("test@example.com");
    });

    it("testConnection does not overwrite zoneId (set via setIdentity, not health)", async () => {
      useGlobalStore.setState({ client: createMockClient(), zoneId: "my-zone" });

      await useGlobalStore.getState().testConnection();

      // zoneId is preserved — no health endpoint provides it
      expect(useGlobalStore.getState().zoneId).toBe("my-zone");
    });

    it("testConnection sets serverVersion from features response", async () => {
      const client = createMockClient();
      useGlobalStore.setState({ client });

      await useGlobalStore.getState().testConnection();

      expect(useGlobalStore.getState().serverVersion).toBe("0.9.0");
    });

    it("transitions through connecting state", async () => {
      const states: string[] = [];
      const unsubscribe = useGlobalStore.subscribe((s) => {
        states.push(s.connectionStatus);
      });

      const client = createMockClient();
      useGlobalStore.setState({ client });

      await useGlobalStore.getState().testConnection();
      unsubscribe();

      // Should have gone through connecting → connected
      expect(states).toContain("connecting");
      expect(states[states.length - 1]).toBe("connected");
    });
  });

  describe("server unreachable → error", () => {
    it("network error sets error status when health fails", async () => {
      const client = createMockClient({
        health: async () => { throw new Error("ECONNREFUSED"); },
        authMe: async () => { throw new Error("ECONNREFUSED"); },
      });
      useGlobalStore.setState({ client });

      await useGlobalStore.getState().testConnection();

      const state = useGlobalStore.getState();
      expect(state.connectionStatus).toBe("error");
      expect(state.connectionError).toBe("Server health check failed");
      expect(state.userInfo).toBeNull();
    });

    it("timeout sets error status when health fails", async () => {
      const client = createMockClient({
        health: async () => { throw new Error("Request timed out"); },
        authMe: async () => { throw new Error("Request timed out"); },
      });
      useGlobalStore.setState({ client });

      await useGlobalStore.getState().testConnection();

      const state = useGlobalStore.getState();
      expect(state.connectionStatus).toBe("error");
      expect(state.connectionError).toBe("Server health check failed");
    });
  });

  describe("auth failures (non-fatal when health passes)", () => {
    it("401 unauthorized still connects if health passes", async () => {
      const client = createMockClient({
        authMe: async () => { throw new Error("Unauthorized (401)"); },
      });
      useGlobalStore.setState({ client });

      await useGlobalStore.getState().testConnection();

      const state = useGlobalStore.getState();
      expect(state.connectionStatus).toBe("connected");
      expect(state.userInfo).toBeNull();
    });

    it("403 forbidden still connects if health passes", async () => {
      const client = createMockClient({
        authMe: async () => { throw new Error("Forbidden (403)"); },
      });
      useGlobalStore.setState({ client });

      await useGlobalStore.getState().testConnection();

      const state = useGlobalStore.getState();
      expect(state.connectionStatus).toBe("connected");
      expect(state.userInfo).toBeNull();
    });
  });

  describe("no client → disconnected", () => {
    it("testConnection with null client stays disconnected", async () => {
      useGlobalStore.setState({ client: null });

      await useGlobalStore.getState().testConnection();

      const state = useGlobalStore.getState();
      expect(state.connectionStatus).toBe("disconnected");
      expect(state.connectionError).toBeNull();
      expect(state.userInfo).toBeNull();
    });
  });

  describe("reconnection", () => {
    it("can recover from error to connected", async () => {
      // First: fail (health must also fail for error status)
      const failClient = createMockClient({
        health: async () => { throw new Error("Connection refused"); },
        authMe: async () => { throw new Error("Connection refused"); },
      });
      useGlobalStore.setState({ client: failClient });
      await useGlobalStore.getState().testConnection();
      expect(useGlobalStore.getState().connectionStatus).toBe("error");

      // Second: succeed
      const okClient = createMockClient();
      useGlobalStore.setState({ client: okClient });
      await useGlobalStore.getState().testConnection();
      expect(useGlobalStore.getState().connectionStatus).toBe("connected");
      expect(useGlobalStore.getState().userInfo).not.toBeNull();
      expect(useGlobalStore.getState().connectionError).toBeNull();
    });
  });

  describe("non-Error thrown objects", () => {
    it("handles string throws when health fails", async () => {
      const client = createMockClient({
        health: async () => { throw "raw string error"; },
        authMe: async () => { throw "raw string error"; },
      });
      useGlobalStore.setState({ client });

      await useGlobalStore.getState().testConnection();

      const state = useGlobalStore.getState();
      expect(state.connectionStatus).toBe("error");
      expect(state.connectionError).toBe("Server health check failed");
    });
  });

  describe("server error (500)", () => {
    it("server error on health sets error status", async () => {
      const client = createMockClient({
        health: async () => { throw new Error("Internal Server Error (500)"); },
        authMe: async () => { throw new Error("Internal Server Error (500)"); },
      });
      useGlobalStore.setState({ client });

      await useGlobalStore.getState().testConnection();

      const state = useGlobalStore.getState();
      expect(state.connectionStatus).toBe("error");
    });
  });

  describe("testConnection with null client (Codex finding 1)", () => {
    it("testConnection returns immediately when client is null", async () => {
      // This validates the bug Codex found: calling testConnection() when
      // client is null does NOT attempt connection — it just stays disconnected.
      // The fix is that PreConnectionScreen must call initConfig() instead.
      useGlobalStore.setState({ client: null, connectionStatus: "disconnected" });

      await useGlobalStore.getState().testConnection();

      const state = useGlobalStore.getState();
      expect(state.connectionStatus).toBe("disconnected");
      expect(state.connectionError).toBeNull();
    });

    it("initConfig re-reads config and can create a new client", () => {
      // Verify that initConfig() with overrides creates a new client
      useGlobalStore.setState({ client: null, connectionStatus: "disconnected" });

      useGlobalStore.getState().initConfig({ apiKey: "sk-new-key", baseUrl: "http://localhost:2026" });

      // Should now have a client and be in connecting state
      const state = useGlobalStore.getState();
      expect(state.client).not.toBeNull();
      expect(state.connectionStatus).toBe("connecting");
    });
  });
});
