/**
 * Tests for stack-store — Docker container status, config/state file reading,
 * and server health details.
 */

import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useStackStore } from "../../src/stores/stack-store.js";
import type { FetchClient } from "@nexus/api-client";

describe("StackStore", () => {
  beforeEach(() => {
    useStackStore.setState({
      activeTab: "containers",
      containers: [],
      containersLoading: false,
      configYaml: "",
      configLoading: false,
      stateJson: null,
      stateLoading: false,
      healthDetails: null,
      healthLoading: false,
      error: null,
      lastRefreshed: 0,
    });
  });

  // ===========================================================================
  // Tab switching
  // ===========================================================================

  describe("setActiveTab", () => {
    it("switches to config tab", () => {
      useStackStore.getState().setActiveTab("config");
      expect(useStackStore.getState().activeTab).toBe("config");
    });

    it("switches to state tab", () => {
      useStackStore.getState().setActiveTab("state");
      expect(useStackStore.getState().activeTab).toBe("state");
    });

    it("switches back to containers tab", () => {
      useStackStore.getState().setActiveTab("state");
      useStackStore.getState().setActiveTab("containers");
      expect(useStackStore.getState().activeTab).toBe("containers");
    });
  });

  // ===========================================================================
  // fetchHealth
  // ===========================================================================

  describe("fetchHealth", () => {
    it("sets healthDetails on success", async () => {
      const mockHealth = {
        status: "healthy",
        service: "nexus-rpc",
        components: {
          search_daemon: { status: "healthy" },
          rebac: { status: "healthy" },
        },
      };
      const mockClient = {
        get: mock(async () => mockHealth),
      } as unknown as FetchClient;

      await useStackStore.getState().fetchHealth(mockClient);
      const state = useStackStore.getState();

      expect(state.healthDetails).not.toBeNull();
      expect(state.healthDetails!.status).toBe("healthy");
      expect(state.healthDetails!.components.search_daemon.status).toBe("healthy");
      expect(state.healthLoading).toBe(false);
    });

    it("falls back to basic health on detailed failure", async () => {
      const callCount = { n: 0 };
      const mockClient = {
        get: mock(async (path: string) => {
          callCount.n++;
          if (path === "/health/detailed") throw new Error("Forbidden");
          return { status: "healthy", service: "nexus-rpc" };
        }),
      } as unknown as FetchClient;

      await useStackStore.getState().fetchHealth(mockClient);
      const state = useStackStore.getState();

      expect(state.healthDetails).not.toBeNull();
      expect(state.healthDetails!.status).toBe("healthy");
      expect(state.healthDetails!.components).toEqual({});
      expect(state.healthLoading).toBe(false);
    });

    it("sets healthDetails to null on complete failure", async () => {
      const mockClient = {
        get: mock(async () => { throw new Error("Network error"); }),
      } as unknown as FetchClient;

      await useStackStore.getState().fetchHealth(mockClient);
      const state = useStackStore.getState();

      expect(state.healthDetails).toBeNull();
      expect(state.healthLoading).toBe(false);
    });

    it("sets healthLoading during fetch", async () => {
      let resolve: () => void;
      const pending = new Promise<void>((r) => { resolve = r; });
      const mockClient = {
        get: mock(async () => {
          await pending;
          return { status: "healthy", service: "nexus-rpc", components: {} };
        }),
      } as unknown as FetchClient;

      const promise = useStackStore.getState().fetchHealth(mockClient);
      expect(useStackStore.getState().healthLoading).toBe(true);

      resolve!();
      await promise;
      expect(useStackStore.getState().healthLoading).toBe(false);
    });
  });

  // ===========================================================================
  // refreshAll
  // ===========================================================================

  describe("refreshAll", () => {
    it("updates lastRefreshed timestamp", async () => {
      const before = Date.now();
      // refreshAll will call fetchContainers, fetchConfig, fetchState which may
      // fail in test env (no Docker, no nexus.yaml), but that's fine — they
      // handle errors gracefully and lastRefreshed should still be set.
      await useStackStore.getState().refreshAll(null);
      const after = Date.now();

      const { lastRefreshed } = useStackStore.getState();
      expect(lastRefreshed).toBeGreaterThanOrEqual(before);
      expect(lastRefreshed).toBeLessThanOrEqual(after);
    });

    it("calls fetchHealth when client is provided", async () => {
      const mockClient = {
        get: mock(async () => ({
          status: "healthy",
          service: "nexus-rpc",
          components: {},
        })),
      } as unknown as FetchClient;

      await useStackStore.getState().refreshAll(mockClient);

      // Should have called get at least once (for /health/detailed)
      expect(mockClient.get).toHaveBeenCalled();
      expect(useStackStore.getState().healthDetails).not.toBeNull();
    });

    it("skips fetchHealth when client is null", async () => {
      await useStackStore.getState().refreshAll(null);
      expect(useStackStore.getState().healthDetails).toBeNull();
    });
  });

  // ===========================================================================
  // Initial state
  // ===========================================================================

  describe("initial state", () => {
    it("starts with containers tab active", () => {
      expect(useStackStore.getState().activeTab).toBe("containers");
    });

    it("starts with empty containers", () => {
      expect(useStackStore.getState().containers).toEqual([]);
    });

    it("starts with no health details", () => {
      expect(useStackStore.getState().healthDetails).toBeNull();
    });

    it("starts with no state json", () => {
      expect(useStackStore.getState().stateJson).toBeNull();
    });

    it("starts with no error", () => {
      expect(useStackStore.getState().error).toBeNull();
    });
  });
});
