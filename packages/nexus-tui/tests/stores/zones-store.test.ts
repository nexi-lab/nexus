import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useZonesStore } from "../../src/stores/zones-store.js";
import type { FetchClient } from "@nexus/api-client";

function mockClient(responses: Record<string, unknown>): FetchClient {
  return {
    get: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked path: ${path}`);
    }),
    post: mock(async (path: string) => {
      for (const [pattern, response] of Object.entries(responses)) {
        if (path.includes(pattern)) return response;
      }
      throw new Error(`Unmocked path: ${path}`);
    }),
  } as unknown as FetchClient;
}

function resetStore(): void {
  useZonesStore.setState({
    zones: [],
    zonesLoading: false,
    bricksHealth: null,
    bricks: [],
    selectedIndex: 0,
    isLoading: false,
    error: null,
    activeTab: "zones",
    brickDetail: null,
    detailLoading: false,
    driftReport: null,
    driftLoading: false,
  });
}

const SAMPLE_ZONES = [
  {
    zone_id: "zone-us-east",
    name: "US East",
    domain: "us-east.nexus.io",
    description: "Primary US zone",
    phase: "active",
    finalizers: ["cleanup"],
    is_active: true,
    created_at: "2025-06-01T10:00:00Z",
    updated_at: "2025-06-01T12:00:00Z",
    limits: null,
  },
  {
    zone_id: "zone-eu-west",
    name: "EU West",
    domain: null,
    description: null,
    phase: "pending",
    finalizers: [],
    is_active: false,
    created_at: "2025-06-01T11:00:00Z",
    updated_at: "2025-06-01T11:00:00Z",
    limits: null,
  },
];

const SAMPLE_BRICKS = [
  {
    name: "brick-alpha",
    state: "running",
    protocol_name: "grpc",
    error: null,
    started_at: 1717243200,
    stopped_at: null,
    unmounted_at: null,
  },
  {
    name: "brick-beta",
    state: "failed",
    protocol_name: "http",
    error: "connection refused",
    started_at: 1717239600,
    stopped_at: 1717243200,
    unmounted_at: null,
  },
  {
    name: "brick-gamma",
    state: "stopped",
    protocol_name: "grpc",
    error: null,
    started_at: null,
    stopped_at: null,
    unmounted_at: 1717243200,
  },
];

const SAMPLE_BRICKS_HEALTH = {
  total: 3,
  active: 1,
  failed: 1,
  bricks: SAMPLE_BRICKS,
};

describe("ZonesStore", () => {
  beforeEach(() => {
    resetStore();
  });

  describe("fetchZones", () => {
    it("fetches and stores zone list", async () => {
      const client = mockClient({
        "/api/zones": { zones: SAMPLE_ZONES, total: 2 },
      });

      await useZonesStore.getState().fetchZones(client);
      const state = useZonesStore.getState();

      expect(state.zones).toHaveLength(2);
      expect(state.zones[0]!.zone_id).toBe("zone-us-east");
      expect(state.zones[0]!.name).toBe("US East");
      expect(state.zones[0]!.is_active).toBe(true);
      expect(state.zones[1]!.phase).toBe("pending");
      expect(state.zonesLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("handles empty zone list", async () => {
      const client = mockClient({
        "/api/zones": { zones: [], total: 0 },
      });

      await useZonesStore.getState().fetchZones(client);
      const state = useZonesStore.getState();

      expect(state.zones).toHaveLength(0);
      expect(state.zonesLoading).toBe(false);
    });

    it("sets error on fetch failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Zone service down"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().fetchZones(client);
      const state = useZonesStore.getState();

      expect(state.zones).toHaveLength(0);
      expect(state.zonesLoading).toBe(false);
      expect(state.error).toBe("Zone service down");
    });
  });

  describe("fetchBricks", () => {
    it("fetches and stores brick list from health endpoint", async () => {
      const client = mockClient({
        "/api/v2/bricks/health": SAMPLE_BRICKS_HEALTH,
      });

      await useZonesStore.getState().fetchBricks(client);
      const state = useZonesStore.getState();

      expect(state.bricks).toHaveLength(3);
      expect(state.bricks[0]!.name).toBe("brick-alpha");
      expect(state.bricks[0]!.state).toBe("running");
      expect(state.bricks[1]!.error).toBe("connection refused");
      expect(state.bricks[2]!.state).toBe("stopped");
      expect(state.bricksHealth!.total).toBe(3);
      expect(state.bricksHealth!.active).toBe(1);
      expect(state.bricksHealth!.failed).toBe(1);
      expect(state.isLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("handles empty brick list", async () => {
      const client = mockClient({
        "/api/v2/bricks/health": { total: 0, active: 0, failed: 0, bricks: [] },
      });

      await useZonesStore.getState().fetchBricks(client);
      const state = useZonesStore.getState();

      expect(state.bricks).toHaveLength(0);
      expect(state.isLoading).toBe(false);
    });

    it("sets error on fetch failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Network timeout"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().fetchBricks(client);
      const state = useZonesStore.getState();

      expect(state.bricks).toHaveLength(0);
      expect(state.isLoading).toBe(false);
      expect(state.error).toBe("Network timeout");
    });

    it("clears previous error on successful fetch", async () => {
      useZonesStore.setState({ error: "old error" });

      const client = mockClient({
        "/api/v2/bricks/health": SAMPLE_BRICKS_HEALTH,
      });

      await useZonesStore.getState().fetchBricks(client);
      expect(useZonesStore.getState().error).toBeNull();
    });
  });

  describe("fetchBrickDetail", () => {
    it("fetches and stores individual brick detail with extended fields", async () => {
      const brickData = {
        name: "brick-alpha",
        state: "running",
        protocol_name: "grpc",
        error: null,
        started_at: 1717243200,
        stopped_at: null,
        unmounted_at: null,
        enabled: true,
        depends_on: ["brick-gamma"],
        depended_by: ["brick-beta"],
        retry_count: 2,
        transitions: [
          { timestamp: 1000.0, event: "mount", from_state: "REGISTERED", to_state: "STARTING" },
          { timestamp: 1001.0, event: "started", from_state: "STARTING", to_state: "ACTIVE" },
        ],
      };

      const client = mockClient({
        "/api/v2/bricks/brick-alpha": brickData,
      });

      await useZonesStore.getState().fetchBrickDetail("brick-alpha", client);
      const state = useZonesStore.getState();

      expect(state.brickDetail).not.toBeNull();
      expect(state.brickDetail!.name).toBe("brick-alpha");
      expect(state.brickDetail!.state).toBe("running");
      expect(state.brickDetail!.protocol_name).toBe("grpc");
      expect(state.brickDetail!.error).toBeNull();
      expect(state.brickDetail!.started_at).toBe(1717243200);
      expect(state.brickDetail!.enabled).toBe(true);
      expect(state.brickDetail!.depends_on).toEqual(["brick-gamma"]);
      expect(state.brickDetail!.depended_by).toEqual(["brick-beta"]);
      expect(state.brickDetail!.retry_count).toBe(2);
      expect(state.brickDetail!.transitions).toHaveLength(2);
      expect(state.brickDetail!.transitions[0]!.event).toBe("mount");
      expect(state.brickDetail!.transitions[0]!.from_state).toBe("REGISTERED");
      expect(state.brickDetail!.transitions[1]!.to_state).toBe("ACTIVE");
      expect(state.detailLoading).toBe(false);
    });

    it("sets error on detail fetch failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Brick not found"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().fetchBrickDetail("brick-missing", client);
      const state = useZonesStore.getState();

      expect(state.brickDetail).toBeNull();
      expect(state.detailLoading).toBe(false);
      expect(state.error).toBe("Brick not found");
    });
  });

  describe("fetchDrift", () => {
    it("fetches and stores drift report", async () => {
      const driftData = {
        total_bricks: 5,
        drifted: 2,
        actions_taken: 1,
        errors: 0,
        drifts: [
          {
            brick_name: "brick-alpha",
            spec_state: "running",
            actual_state: "stopped",
            action: "restart",
            detail: "Process exited unexpectedly",
          },
          {
            brick_name: "brick-beta",
            spec_state: "running",
            actual_state: "failed",
            action: "reset",
            detail: "Connection refused",
          },
        ],
        last_reconcile_at: 1717243200,
        reconcile_count: 42,
      };

      const client = mockClient({
        "/api/v2/bricks/drift": driftData,
      });

      await useZonesStore.getState().fetchDrift(client);
      const state = useZonesStore.getState();

      expect(state.driftReport).not.toBeNull();
      expect(state.driftReport!.total_bricks).toBe(5);
      expect(state.driftReport!.drifted).toBe(2);
      expect(state.driftReport!.actions_taken).toBe(1);
      expect(state.driftReport!.errors).toBe(0);
      expect(state.driftReport!.drifts).toHaveLength(2);
      expect(state.driftReport!.drifts[0]!.brick_name).toBe("brick-alpha");
      expect(state.driftReport!.drifts[0]!.action).toBe("restart");
      expect(state.driftReport!.reconcile_count).toBe(42);
      expect(state.driftReport!.last_reconcile_at).toBe(1717243200);
      expect(state.driftLoading).toBe(false);
    });

    it("fetches drift report with no drifts", async () => {
      const driftData = {
        total_bricks: 3,
        drifted: 0,
        actions_taken: 0,
        errors: 0,
        drifts: [],
        last_reconcile_at: null,
        reconcile_count: 0,
      };

      const client = mockClient({
        "/api/v2/bricks/drift": driftData,
      });

      await useZonesStore.getState().fetchDrift(client);
      const state = useZonesStore.getState();

      expect(state.driftReport).not.toBeNull();
      expect(state.driftReport!.drifted).toBe(0);
      expect(state.driftReport!.drifts).toHaveLength(0);
      expect(state.driftReport!.last_reconcile_at).toBeNull();
    });

    it("sets error on drift fetch failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Drift service unavailable"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().fetchDrift(client);
      const state = useZonesStore.getState();

      expect(state.driftReport).toBeNull();
      expect(state.driftLoading).toBe(false);
      expect(state.error).toBe("Drift service unavailable");
    });
  });

  describe("remountBrick", () => {
    it("calls POST remount and refreshes bricks list", async () => {
      const client = mockClient({
        "/api/v2/bricks/brick-alpha/remount": undefined,
        "/api/v2/bricks/health": SAMPLE_BRICKS_HEALTH,
      });

      await useZonesStore.getState().remountBrick("brick-alpha", client);
      const state = useZonesStore.getState();

      expect(state.error).toBeNull();
      expect(state.bricks).toHaveLength(3);
      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    });

    it("sets error on remount failure", async () => {
      const client = {
        get: mock(async () => ({ total: 0, active: 0, failed: 0, bricks: [] })),
        post: mock(async () => { throw new Error("Remount rejected"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().remountBrick("brick-alpha", client);
      const state = useZonesStore.getState();

      expect(state.error).toBe("Remount rejected");
    });
  });

  describe("resetBrick", () => {
    it("calls POST reset and refreshes bricks list", async () => {
      const client = mockClient({
        "/api/v2/bricks/brick-beta/reset": undefined,
        "/api/v2/bricks/health": SAMPLE_BRICKS_HEALTH,
      });

      await useZonesStore.getState().resetBrick("brick-beta", client);
      const state = useZonesStore.getState();

      expect(state.error).toBeNull();
      expect(state.bricks).toHaveLength(3);
      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    });

    it("sets error on reset failure", async () => {
      const client = {
        get: mock(async () => ({ total: 0, active: 0, failed: 0, bricks: [] })),
        post: mock(async () => { throw new Error("Reset failed"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().resetBrick("brick-beta", client);
      const state = useZonesStore.getState();

      expect(state.error).toBe("Reset failed");
    });
  });

  describe("setSelectedIndex", () => {
    it("sets the selected index", () => {
      useZonesStore.setState({ bricks: SAMPLE_BRICKS });

      useZonesStore.getState().setSelectedIndex(1);
      const state = useZonesStore.getState();

      expect(state.selectedIndex).toBe(1);
    });

    it("clears brick detail on selection change", () => {
      useZonesStore.setState({
        bricks: SAMPLE_BRICKS,
        brickDetail: SAMPLE_BRICKS[0]!,
      });

      useZonesStore.getState().setSelectedIndex(2);
      const state = useZonesStore.getState();

      expect(state.brickDetail).toBeNull();
    });
  });

  describe("setActiveTab", () => {
    it("switches between tabs", () => {
      useZonesStore.getState().setActiveTab("bricks");
      expect(useZonesStore.getState().activeTab).toBe("bricks");

      useZonesStore.getState().setActiveTab("drift");
      expect(useZonesStore.getState().activeTab).toBe("drift");

      useZonesStore.getState().setActiveTab("zones");
      expect(useZonesStore.getState().activeTab).toBe("zones");
    });
  });

  describe("error handling", () => {
    it("fetchBricks clears error before fetching", async () => {
      useZonesStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/bricks/health": { total: 0, active: 0, failed: 0, bricks: [] },
      });

      await useZonesStore.getState().fetchBricks(client);
      expect(useZonesStore.getState().error).toBeNull();
    });

    it("remountBrick clears error before request", async () => {
      useZonesStore.setState({ error: "old remount error" });

      const client = mockClient({
        "/api/v2/bricks/brick-alpha/remount": undefined,
        "/api/v2/bricks/health": { total: 0, active: 0, failed: 0, bricks: [] },
      });

      await useZonesStore.getState().remountBrick("brick-alpha", client);
      expect(useZonesStore.getState().error).toBeNull();
    });

    it("non-Error exceptions produce fallback message", async () => {
      const client = {
        get: mock(async () => { throw "string error"; }),
      } as unknown as FetchClient;

      await useZonesStore.getState().fetchBricks(client);
      expect(useZonesStore.getState().error).toBe("Failed to fetch bricks");
    });
  });

  describe("mountBrick", () => {
    it("calls POST mount and refreshes bricks list", async () => {
      const client = mockClient({
        "/api/v2/bricks/brick-alpha/mount": undefined,
        "/api/v2/bricks/health": SAMPLE_BRICKS_HEALTH,
      });

      await useZonesStore.getState().mountBrick("brick-alpha", client);
      const state = useZonesStore.getState();

      expect(state.error).toBeNull();
      expect(state.bricks).toHaveLength(3);
      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    });

    it("sets error on mount failure", async () => {
      const client = {
        get: mock(async () => ({ total: 0, active: 0, failed: 0, bricks: [] })),
        post: mock(async () => { throw new Error("Invalid state transition"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().mountBrick("brick-alpha", client);
      const state = useZonesStore.getState();

      expect(state.error).toBe("Invalid state transition");
    });

    it("clears previous error before mount", async () => {
      useZonesStore.setState({ error: "old error" });

      const client = mockClient({
        "/api/v2/bricks/brick-alpha/mount": undefined,
        "/api/v2/bricks/health": { total: 0, active: 0, failed: 0, bricks: [] },
      });

      await useZonesStore.getState().mountBrick("brick-alpha", client);
      expect(useZonesStore.getState().error).toBeNull();
    });
  });

  describe("unmountBrick", () => {
    it("calls POST unmount and refreshes bricks list", async () => {
      const client = mockClient({
        "/api/v2/bricks/brick-alpha/unmount": undefined,
        "/api/v2/bricks/health": SAMPLE_BRICKS_HEALTH,
      });

      await useZonesStore.getState().unmountBrick("brick-alpha", client);
      const state = useZonesStore.getState();

      expect(state.error).toBeNull();
      expect(state.bricks).toHaveLength(3);
      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    });

    it("sets error on unmount failure", async () => {
      const client = {
        get: mock(async () => ({ total: 0, active: 0, failed: 0, bricks: [] })),
        post: mock(async () => { throw new Error("Brick is not active"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().unmountBrick("brick-alpha", client);
      const state = useZonesStore.getState();

      expect(state.error).toBe("Brick is not active");
    });
  });

  describe("unregisterBrick", () => {
    it("calls POST unregister and refreshes bricks list", async () => {
      const client = mockClient({
        "/api/v2/bricks/brick-gamma/unregister": undefined,
        "/api/v2/bricks/health": SAMPLE_BRICKS_HEALTH,
      });

      await useZonesStore.getState().unregisterBrick("brick-gamma", client);
      const state = useZonesStore.getState();

      expect(state.error).toBeNull();
      expect(state.bricks).toHaveLength(3);
      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    });

    it("sets error on unregister failure", async () => {
      const client = {
        get: mock(async () => ({ total: 0, active: 0, failed: 0, bricks: [] })),
        post: mock(async () => { throw new Error("Brick must be unmounted"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().unregisterBrick("brick-gamma", client);
      const state = useZonesStore.getState();

      expect(state.error).toBe("Brick must be unmounted");
    });

    it("non-Error exceptions produce fallback message", async () => {
      const client = {
        get: mock(async () => ({ total: 0, active: 0, failed: 0, bricks: [] })),
        post: mock(async () => { throw 42; }),
      } as unknown as FetchClient;

      await useZonesStore.getState().unregisterBrick("brick-gamma", client);
      expect(useZonesStore.getState().error).toBe("Failed to unregister brick");
    });
  });
});
