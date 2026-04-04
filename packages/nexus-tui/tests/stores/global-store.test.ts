import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useGlobalStore } from "../../src/stores/global-store.js";
import type { FetchClient } from "@nexus-ai-fs/api-client";

describe("GlobalStore", () => {
  beforeEach(() => {
    // Reset store to initial state
    useGlobalStore.setState({
      connectionStatus: "disconnected",
      connectionError: null,
      client: null,
      activePanel: "files",
      panelHistory: [],
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

  describe("setActivePanel", () => {
    it("changes the active panel", () => {
      useGlobalStore.getState().setActivePanel("console");
      expect(useGlobalStore.getState().activePanel).toBe("console");
    });

    it("records previous panel in history", () => {
      useGlobalStore.getState().setActivePanel("console");
      expect(useGlobalStore.getState().panelHistory).toEqual(["files"]);
    });

    it("no-ops when selecting the same panel", () => {
      useGlobalStore.getState().setActivePanel("files");
      expect(useGlobalStore.getState().panelHistory).toEqual([]);
    });

    it("limits history to 10 entries", () => {
      const panels = ["console", "agents", "zones", "access", "payments",
        "search", "workflows", "infrastructure", "files", "console", "agents"] as const;
      for (const panel of panels) {
        useGlobalStore.getState().setActivePanel(panel);
      }
      expect(useGlobalStore.getState().panelHistory.length).toBeLessThanOrEqual(10);
    });

    it("does NOT call refreshFeatures on panel switch (Decision 4A)", async () => {
      // Ensure featuresLastFetched is 0 (TTL expired) so refreshFeatures would
      // actually fire an API call if it were triggered.
      const mockFetchClient = {
        get: mock(async () => ({
          profile: "full",
          mode: "standalone",
          enabled_bricks: ["new_brick"],
          disabled_bricks: [],
          version: null,
          rate_limit_enabled: false,
        })),
      } as unknown as import("@nexus-ai-fs/api-client").FetchClient;

      useGlobalStore.setState({ client: mockFetchClient, featuresLastFetched: 0 });
      useGlobalStore.getState().setActivePanel("console");

      // Give any async refreshFeatures a tick to fire
      await new Promise((r) => setTimeout(r, 10));
      expect((mockFetchClient.get as ReturnType<typeof mock>)).not.toHaveBeenCalled();
    });
  });

  describe("setConnectionStatus", () => {
    it("sets status and error", () => {
      useGlobalStore.getState().setConnectionStatus("error", "Connection refused");
      const state = useGlobalStore.getState();
      expect(state.connectionStatus).toBe("error");
      expect(state.connectionError).toBe("Connection refused");
    });

    it("clears error when no error message", () => {
      useGlobalStore.getState().setConnectionStatus("error", "fail");
      useGlobalStore.getState().setConnectionStatus("connected");
      expect(useGlobalStore.getState().connectionError).toBeNull();
    });

    it("calls refreshFeatures when transitioning to connected (Decision 4A)", async () => {
      const mockFetchClient = {
        get: mock(async () => ({
          profile: "full",
          mode: "standalone",
          enabled_bricks: ["brick_a"],
          disabled_bricks: [],
          version: null,
          rate_limit_enabled: false,
        })),
      } as unknown as import("@nexus-ai-fs/api-client").FetchClient;

      // Start from disconnected with expired TTL so the fetch actually fires
      useGlobalStore.setState({
        client: mockFetchClient,
        connectionStatus: "disconnected",
        featuresLastFetched: 0,
        featuresLoaded: false,
        enabledBricks: [],
      });

      useGlobalStore.getState().setConnectionStatus("connected");
      // refreshFeatures is async; wait for it to settle
      await new Promise((r) => setTimeout(r, 20));

      expect((mockFetchClient.get as ReturnType<typeof mock>)).toHaveBeenCalled();
      expect(useGlobalStore.getState().enabledBricks).toEqual(["brick_a"]);
    });

    it("does NOT call refreshFeatures when already connected", async () => {
      const mockFetchClient = {
        get: mock(async () => ({
          profile: "full",
          mode: "standalone",
          enabled_bricks: ["new"],
          disabled_bricks: [],
          version: null,
          rate_limit_enabled: false,
        })),
      } as unknown as import("@nexus-ai-fs/api-client").FetchClient;

      // Already connected, TTL expired
      useGlobalStore.setState({
        client: mockFetchClient,
        connectionStatus: "connected",
        featuresLastFetched: 0,
      });

      useGlobalStore.getState().setConnectionStatus("connected");
      await new Promise((r) => setTimeout(r, 20));

      // No transition from non-connected, so refreshFeatures should NOT fire
      expect((mockFetchClient.get as ReturnType<typeof mock>)).not.toHaveBeenCalled();
    });
  });

  describe("initConfig", () => {
    it("creates a client even without an API key", () => {
      useGlobalStore.getState().initConfig({ baseUrl: "http://localhost:2026", apiKey: "" });
      const state = useGlobalStore.getState();
      expect(state.client).not.toBeNull();
      expect(state.connectionStatus).toBe("connecting");
    });
  });

  describe("setServerInfo", () => {
    it("sets version, zoneId, uptime", () => {
      useGlobalStore.getState().setServerInfo({
        version: "0.7.2",
        zoneId: "org_acme",
        uptime: 3600,
      });
      const state = useGlobalStore.getState();
      expect(state.serverVersion).toBe("0.7.2");
      expect(state.zoneId).toBe("org_acme");
      expect(state.uptime).toBe(3600);
    });

    it("preserves unset fields", () => {
      useGlobalStore.getState().setServerInfo({ version: "1.0" });
      useGlobalStore.getState().setServerInfo({ zoneId: "z1" });
      const state = useGlobalStore.getState();
      expect(state.serverVersion).toBe("1.0");
      expect(state.zoneId).toBe("z1");
    });
  });

  describe("setFeatures", () => {
    it("sets enabledBricks, profile, mode", () => {
      useGlobalStore.getState().setFeatures({
        profile: "full",
        mode: "standalone",
        enabled_bricks: ["search", "catalog", "pay"],
        disabled_bricks: ["mcp"],
        version: "0.8.0",
        rate_limit_enabled: false,
      });
      const state = useGlobalStore.getState();
      expect(state.enabledBricks).toEqual(["search", "catalog", "pay"]);
      expect(state.profile).toBe("full");
      expect(state.mode).toBe("standalone");
    });

    it("sets featuresLoaded to true", () => {
      expect(useGlobalStore.getState().featuresLoaded).toBe(false);
      useGlobalStore.getState().setFeatures({
        profile: "lite",
        mode: "standalone",
        enabled_bricks: [],
        disabled_bricks: [],
        version: null,
        rate_limit_enabled: false,
      });
      expect(useGlobalStore.getState().featuresLoaded).toBe(true);
    });

    it("updates featuresLastFetched timestamp", () => {
      const before = Date.now();
      useGlobalStore.getState().setFeatures({
        profile: "lite",
        mode: "standalone",
        enabled_bricks: [],
        disabled_bricks: [],
        version: null,
        rate_limit_enabled: false,
      });
      const after = Date.now();
      const { featuresLastFetched } = useGlobalStore.getState();
      expect(featuresLastFetched).toBeGreaterThanOrEqual(before);
      expect(featuresLastFetched).toBeLessThanOrEqual(after);
    });

    it("handles null enabled_bricks gracefully", () => {
      useGlobalStore.getState().setFeatures({
        profile: "lite",
        mode: "standalone",
        enabled_bricks: null as unknown as string[],
        disabled_bricks: [],
        version: null,
        rate_limit_enabled: false,
      });
      expect(useGlobalStore.getState().enabledBricks).toEqual([]);
    });
  });

  describe("refreshFeatures", () => {
    it("skips refresh when no client", async () => {
      useGlobalStore.setState({ client: null, featuresLastFetched: 0 });
      await useGlobalStore.getState().refreshFeatures();
      expect(useGlobalStore.getState().featuresLoaded).toBe(false);
    });

    it("skips refresh when within TTL (10s)", async () => {
      const mockFetchClient = {
        get: mock(async () => ({
          profile: "full",
          mode: "standalone",
          enabled_bricks: ["new_brick"],
          disabled_bricks: [],
          version: null,
          rate_limit_enabled: false,
        })),
      } as unknown as FetchClient;

      useGlobalStore.setState({
        client: mockFetchClient,
        featuresLastFetched: Date.now(), // just fetched
        featuresLoaded: true,
        enabledBricks: ["old_brick"],
      });

      await useGlobalStore.getState().refreshFeatures();
      // Should NOT have called the API
      expect(useGlobalStore.getState().enabledBricks).toEqual(["old_brick"]);
    });

    it("refreshes when TTL has expired", async () => {
      const mockFetchClient = {
        get: mock(async () => ({
          profile: "full",
          mode: "standalone",
          enabled_bricks: ["new_brick"],
          disabled_bricks: [],
          version: null,
          rate_limit_enabled: false,
        })),
      } as unknown as FetchClient;

      useGlobalStore.setState({
        client: mockFetchClient,
        featuresLastFetched: Date.now() - 35_000, // 35s ago, past 30s TTL
        featuresLoaded: true,
        enabledBricks: ["old_brick"],
      });

      await useGlobalStore.getState().refreshFeatures();
      expect(useGlobalStore.getState().enabledBricks).toEqual(["new_brick"]);
    });

    it("handles fetch errors gracefully (keeps last known state)", async () => {
      const mockFetchClient = {
        get: mock(async () => { throw new Error("Network error"); }),
      } as unknown as FetchClient;

      useGlobalStore.setState({
        client: mockFetchClient,
        featuresLastFetched: 0,
        featuresLoaded: true,
        enabledBricks: ["existing_brick"],
      });

      await useGlobalStore.getState().refreshFeatures();
      // Should keep the existing bricks
      expect(useGlobalStore.getState().enabledBricks).toEqual(["existing_brick"]);
    });
  });

  describe("testConnection", () => {
    it("sets connected and userInfo on success", async () => {
      const mockHealth = { status: "ready", uptime_seconds: 100 };
      const mockUserInfo = {
        user_id: "user-1",
        email: "test@example.com",
        username: "testuser",
        display_name: "Test User",
        avatar_url: null,
        is_global_admin: false,
        primary_auth_method: "api_key",
      };

      // testConnection calls client.get 3 times: health, features, auth/me
      const mockGet = mock()
        .mockResolvedValueOnce(mockHealth)     // health (/healthz/ready)
        .mockResolvedValueOnce(null)           // features
        .mockResolvedValueOnce(mockUserInfo);  // auth/me

      const mockFetchClient = { get: mockGet } as unknown as FetchClient;
      useGlobalStore.setState({ client: mockFetchClient });

      await useGlobalStore.getState().testConnection();
      const state = useGlobalStore.getState();

      expect(state.connectionStatus).toBe("connected");
      expect(state.connectionError).toBeNull();
      expect(state.userInfo).not.toBeNull();
      expect(state.userInfo!.user_id).toBe("user-1");
      expect(state.userInfo!.email).toBe("test@example.com");
      expect(state.userInfo!.is_global_admin).toBe(false);
    });

    it("sets connected when health passes but auth/me fails", async () => {
      const mockHealth = { status: "ready", uptime_seconds: 10 };
      const mockGet = mock()
        .mockResolvedValueOnce(mockHealth)     // health OK
        .mockResolvedValueOnce(null)           // features
        .mockRejectedValueOnce(new Error("Auth not configured")); // auth/me fails

      const mockFetchClient = { get: mockGet } as unknown as FetchClient;
      useGlobalStore.setState({ client: mockFetchClient });

      await useGlobalStore.getState().testConnection();
      const state = useGlobalStore.getState();

      // Server is connected if health passes, even when auth fails
      expect(state.connectionStatus).toBe("connected");
      expect(state.userInfo).toBeNull();
    });

    it("sets error status when health fails", async () => {
      // All 3 calls fail (server unreachable) → health is null → error
      const mockFetchClient = {
        get: mock(async () => {
          throw new Error("Network error");
        }),
      } as unknown as FetchClient;

      useGlobalStore.setState({ client: mockFetchClient });

      await useGlobalStore.getState().testConnection();
      const state = useGlobalStore.getState();

      expect(state.connectionStatus).toBe("error");
      expect(state.connectionError).toBe("Server health check failed");
      expect(state.userInfo).toBeNull();
    });

    it("sets disconnected when no client", async () => {
      useGlobalStore.setState({ client: null });

      await useGlobalStore.getState().testConnection();
      const state = useGlobalStore.getState();

      expect(state.connectionStatus).toBe("disconnected");
      expect(state.connectionError).toBeNull();
      expect(state.userInfo).toBeNull();
    });

    it("handles non-Error thrown objects", async () => {
      const mockFetchClient = {
        get: mock(async () => { throw "string error"; }),
      } as unknown as FetchClient;

      useGlobalStore.setState({ client: mockFetchClient });

      await useGlobalStore.getState().testConnection();
      const state = useGlobalStore.getState();

      expect(state.connectionStatus).toBe("error");
      expect(state.connectionError).toBe("Server health check failed");
    });
  });
});
