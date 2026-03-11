import { describe, it, expect, beforeEach } from "bun:test";
import { useGlobalStore } from "../../src/stores/global-store.js";

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
});
