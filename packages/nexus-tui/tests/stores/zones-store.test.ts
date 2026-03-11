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
    bricks: [],
    selectedBrick: null,
    selectedIndex: 0,
    isLoading: false,
    error: null,
    activeTab: "overview",
    brickHealth: null,
    healthLoading: false,
    mountPoints: [],
    mountsLoading: false,
    driftReport: null,
    driftLoading: false,
  });
}

const SAMPLE_BRICKS = [
  {
    brick_id: "brick-1",
    zone_id: "zone-us-east",
    brick_type: "storage",
    status: "online" as const,
    address: "10.0.1.1:9000",
    capacity_bytes: 1073741824,
    used_bytes: 536870912,
    last_seen: "2025-06-01T12:00:00Z",
  },
  {
    brick_id: "brick-2",
    zone_id: "zone-eu-west",
    brick_type: "cache",
    status: "degraded" as const,
    address: "10.0.2.1:9000",
    capacity_bytes: 2147483648,
    used_bytes: 107374182,
    last_seen: "2025-06-01T11:55:00Z",
  },
  {
    brick_id: "brick-3",
    zone_id: "zone-us-east",
    brick_type: "storage",
    status: "offline" as const,
    address: "10.0.1.2:9000",
    capacity_bytes: 1073741824,
    used_bytes: 0,
    last_seen: null,
  },
];

describe("ZonesStore", () => {
  beforeEach(() => {
    resetStore();
  });

  describe("fetchBricks", () => {
    it("fetches and stores brick list", async () => {
      const client = mockClient({
        "/api/v2/bricks": { bricks: SAMPLE_BRICKS, total: 3 },
      });

      await useZonesStore.getState().fetchBricks(client);
      const state = useZonesStore.getState();

      expect(state.bricks).toHaveLength(3);
      expect(state.bricks[0]!.brick_id).toBe("brick-1");
      expect(state.bricks[1]!.zone_id).toBe("zone-eu-west");
      expect(state.bricks[2]!.status).toBe("offline");
      expect(state.isLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("handles empty brick list", async () => {
      const client = mockClient({
        "/api/v2/bricks": { bricks: [], total: 0 },
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
        "/api/v2/bricks": { bricks: SAMPLE_BRICKS, total: 3 },
      });

      await useZonesStore.getState().fetchBricks(client);
      expect(useZonesStore.getState().error).toBeNull();
    });
  });

  describe("fetchBrickHealth", () => {
    it("fetches and stores brick health", async () => {
      const healthData = {
        brick_id: "brick-1",
        status: "healthy",
        latency_ms: 12,
        error_rate: 0.001,
        last_check: "2025-06-01T12:00:00Z",
        checks: [
          { name: "disk", status: "pass", message: "OK" },
          { name: "network", status: "pass", message: "OK" },
          { name: "memory", status: "warn", message: "High usage" },
        ],
      };

      const client = mockClient({
        "/api/v2/bricks/brick-1/health": healthData,
      });

      await useZonesStore.getState().fetchBrickHealth("brick-1", client);
      const state = useZonesStore.getState();

      expect(state.brickHealth).not.toBeNull();
      expect(state.brickHealth!.brick_id).toBe("brick-1");
      expect(state.brickHealth!.status).toBe("healthy");
      expect(state.brickHealth!.latency_ms).toBe(12);
      expect(state.brickHealth!.error_rate).toBe(0.001);
      expect(state.brickHealth!.checks).toHaveLength(3);
      expect(state.brickHealth!.checks[2]!.status).toBe("warn");
      expect(state.healthLoading).toBe(false);
    });

    it("sets error on health fetch failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Health check unavailable"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().fetchBrickHealth("brick-1", client);
      const state = useZonesStore.getState();

      expect(state.brickHealth).toBeNull();
      expect(state.healthLoading).toBe(false);
      expect(state.error).toBe("Health check unavailable");
    });
  });

  describe("fetchMounts", () => {
    it("fetches and stores mount points", async () => {
      const mountData = {
        mounts: [
          {
            path: "/data/storage",
            brick_id: "brick-1",
            zone_id: "zone-us-east",
            mount_type: "readwrite",
            mounted_at: "2025-06-01T10:00:00Z",
          },
          {
            path: "/cache/hot",
            brick_id: "brick-1",
            zone_id: "zone-us-east",
            mount_type: "read",
            mounted_at: "2025-06-01T10:05:00Z",
          },
        ],
      };

      const client = mockClient({
        "/api/v2/bricks/brick-1/mount": mountData,
      });

      await useZonesStore.getState().fetchMounts("brick-1", client);
      const state = useZonesStore.getState();

      expect(state.mountPoints).toHaveLength(2);
      expect(state.mountPoints[0]!.path).toBe("/data/storage");
      expect(state.mountPoints[0]!.mount_type).toBe("readwrite");
      expect(state.mountPoints[1]!.path).toBe("/cache/hot");
      expect(state.mountsLoading).toBe(false);
    });

    it("sets error on mount fetch failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Mount service down"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().fetchMounts("brick-1", client);
      const state = useZonesStore.getState();

      expect(state.mountPoints).toHaveLength(0);
      expect(state.mountsLoading).toBe(false);
      expect(state.error).toBe("Mount service down");
    });
  });

  describe("fetchDrift", () => {
    it("fetches and stores drift report", async () => {
      const driftData = {
        brick_id: "brick-1",
        has_drift: true,
        drift_count: 3,
        last_checked: "2025-06-01T12:00:00Z",
        drifted_paths: ["/data/file1.bin", "/data/file2.bin", "/config/settings.json"],
      };

      const client = mockClient({
        "/api/v2/bricks/brick-1/drift": driftData,
      });

      await useZonesStore.getState().fetchDrift("brick-1", client);
      const state = useZonesStore.getState();

      expect(state.driftReport).not.toBeNull();
      expect(state.driftReport!.brick_id).toBe("brick-1");
      expect(state.driftReport!.has_drift).toBe(true);
      expect(state.driftReport!.drift_count).toBe(3);
      expect(state.driftReport!.drifted_paths).toHaveLength(3);
      expect(state.driftReport!.drifted_paths[0]).toBe("/data/file1.bin");
      expect(state.driftLoading).toBe(false);
    });

    it("fetches drift report with no drift", async () => {
      const driftData = {
        brick_id: "brick-2",
        has_drift: false,
        drift_count: 0,
        last_checked: "2025-06-01T12:00:00Z",
        drifted_paths: [],
      };

      const client = mockClient({
        "/api/v2/bricks/brick-2/drift": driftData,
      });

      await useZonesStore.getState().fetchDrift("brick-2", client);
      const state = useZonesStore.getState();

      expect(state.driftReport).not.toBeNull();
      expect(state.driftReport!.has_drift).toBe(false);
      expect(state.driftReport!.drift_count).toBe(0);
      expect(state.driftReport!.drifted_paths).toHaveLength(0);
    });

    it("sets error on drift fetch failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Drift service unavailable"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().fetchDrift("brick-1", client);
      const state = useZonesStore.getState();

      expect(state.driftReport).toBeNull();
      expect(state.driftLoading).toBe(false);
      expect(state.error).toBe("Drift service unavailable");
    });
  });

  describe("triggerSync", () => {
    it("calls POST and refreshes bricks list", async () => {
      const client = mockClient({
        "/api/v2/bricks/brick-1/sync": undefined,
        "/api/v2/bricks": { bricks: SAMPLE_BRICKS, total: 3 },
      });

      await useZonesStore.getState().triggerSync("brick-1", client);
      const state = useZonesStore.getState();

      expect(state.error).toBeNull();
      expect(state.bricks).toHaveLength(3);
      expect((client.post as ReturnType<typeof mock>).mock.calls.length).toBe(1);
    });

    it("sets error on sync failure", async () => {
      const client = {
        get: mock(async () => ({ bricks: [], total: 0 })),
        post: mock(async () => { throw new Error("Sync rejected"); }),
      } as unknown as FetchClient;

      await useZonesStore.getState().triggerSync("brick-1", client);
      const state = useZonesStore.getState();

      expect(state.error).toBe("Sync rejected");
    });
  });

  describe("setSelectedIndex", () => {
    it("sets the selected index and brick", () => {
      useZonesStore.setState({ bricks: SAMPLE_BRICKS });

      useZonesStore.getState().setSelectedIndex(1);
      const state = useZonesStore.getState();

      expect(state.selectedIndex).toBe(1);
      expect(state.selectedBrick).not.toBeNull();
      expect(state.selectedBrick!.brick_id).toBe("brick-2");
    });

    it("sets selectedBrick to null for out-of-range index", () => {
      useZonesStore.setState({ bricks: SAMPLE_BRICKS });

      useZonesStore.getState().setSelectedIndex(99);
      const state = useZonesStore.getState();

      expect(state.selectedIndex).toBe(99);
      expect(state.selectedBrick).toBeNull();
    });

    it("clears detail data on selection change", () => {
      useZonesStore.setState({
        bricks: SAMPLE_BRICKS,
        brickHealth: {
          brick_id: "brick-1",
          status: "healthy",
          latency_ms: 5,
          error_rate: 0,
          last_check: "2025-06-01T12:00:00Z",
          checks: [],
        },
        mountPoints: [
          {
            path: "/data",
            brick_id: "brick-1",
            zone_id: "zone-us-east",
            mount_type: "read",
            mounted_at: "2025-06-01T10:00:00Z",
          },
        ],
        driftReport: {
          brick_id: "brick-1",
          has_drift: false,
          drift_count: 0,
          last_checked: "2025-06-01T12:00:00Z",
          drifted_paths: [],
        },
      });

      useZonesStore.getState().setSelectedIndex(2);
      const state = useZonesStore.getState();

      expect(state.brickHealth).toBeNull();
      expect(state.mountPoints).toHaveLength(0);
      expect(state.driftReport).toBeNull();
    });
  });

  describe("setActiveTab", () => {
    it("switches between tabs", () => {
      useZonesStore.getState().setActiveTab("health");
      expect(useZonesStore.getState().activeTab).toBe("health");

      useZonesStore.getState().setActiveTab("mounts");
      expect(useZonesStore.getState().activeTab).toBe("mounts");

      useZonesStore.getState().setActiveTab("drift");
      expect(useZonesStore.getState().activeTab).toBe("drift");

      useZonesStore.getState().setActiveTab("overview");
      expect(useZonesStore.getState().activeTab).toBe("overview");
    });
  });

  describe("error handling", () => {
    it("fetchBricks clears error before fetching", async () => {
      useZonesStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/bricks": { bricks: [], total: 0 },
      });

      await useZonesStore.getState().fetchBricks(client);
      expect(useZonesStore.getState().error).toBeNull();
    });

    it("triggerSync clears error before request", async () => {
      useZonesStore.setState({ error: "old sync error" });

      const client = mockClient({
        "/api/v2/bricks/brick-1/sync": undefined,
        "/api/v2/bricks": { bricks: [], total: 0 },
      });

      await useZonesStore.getState().triggerSync("brick-1", client);
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
});
