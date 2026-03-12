import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useGlobalStore } from "../../src/stores/global-store.js";
import type { FetchClient } from "@nexus/api-client";

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

  describe("testConnection", () => {
    it("sets connected and userInfo on success", async () => {
      const mockUserInfo = {
        user_id: "user-1",
        email: "test@example.com",
        username: "testuser",
        display_name: "Test User",
        avatar_url: null,
        is_global_admin: false,
        primary_auth_method: "api_key",
      };

      const mockFetchClient = {
        get: mock(async () => mockUserInfo),
      } as unknown as FetchClient;

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

    it("sets error status on failure", async () => {
      const mockFetchClient = {
        get: mock(async () => {
          throw new Error("Unauthorized");
        }),
      } as unknown as FetchClient;

      useGlobalStore.setState({ client: mockFetchClient });

      await useGlobalStore.getState().testConnection();
      const state = useGlobalStore.getState();

      expect(state.connectionStatus).toBe("error");
      expect(state.connectionError).toBe("Unauthorized");
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
      expect(state.connectionError).toBe("Connection test failed");
    });
  });
});
