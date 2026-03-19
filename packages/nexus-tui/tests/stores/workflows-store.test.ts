import { describe, it, expect, beforeEach, mock } from "bun:test";
import { useWorkflowsStore } from "../../src/stores/workflows-store.js";
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
  useWorkflowsStore.setState({
    workflows: [],
    selectedWorkflowIndex: 0,
    workflowsLoading: false,
    selectedWorkflow: null,
    detailLoading: false,
    executions: [],
    selectedExecutionIndex: 0,
    executionsLoading: false,
    schedulerMetrics: null,
    schedulerLoading: false,
    activeTab: "workflows",
    error: null,
  });
}

describe("WorkflowsStore", () => {
  beforeEach(() => {
    resetStore();
  });

  // =========================================================================
  // setActiveTab
  // =========================================================================

  describe("setActiveTab", () => {
    it("switches between tabs", () => {
      useWorkflowsStore.getState().setActiveTab("executions");
      expect(useWorkflowsStore.getState().activeTab).toBe("executions");

      useWorkflowsStore.getState().setActiveTab("scheduler");
      expect(useWorkflowsStore.getState().activeTab).toBe("scheduler");

      useWorkflowsStore.getState().setActiveTab("workflows");
      expect(useWorkflowsStore.getState().activeTab).toBe("workflows");
    });

    it("clears error when switching tabs", () => {
      useWorkflowsStore.setState({ error: "previous error" });
      useWorkflowsStore.getState().setActiveTab("scheduler");
      expect(useWorkflowsStore.getState().error).toBeNull();
    });
  });

  // =========================================================================
  // fetchWorkflows
  // =========================================================================

  describe("fetchWorkflows", () => {
    it("fetches and stores workflows from array response", async () => {
      const client = mockClient({
        "/api/v2/workflows": [
          {
            name: "deploy-pipeline",
            version: "1.0.0",
            description: "Deploys to production",
            enabled: true,
            triggers: 2,
            actions: 5,
          },
          {
            name: "data-sync",
            version: "2.1.0",
            description: null,
            enabled: false,
            triggers: 1,
            actions: 3,
          },
        ],
      });

      await useWorkflowsStore.getState().fetchWorkflows(client);
      const state = useWorkflowsStore.getState();

      expect(state.workflows).toHaveLength(2);
      expect(state.workflows[0]!.name).toBe("deploy-pipeline");
      expect(state.workflows[0]!.version).toBe("1.0.0");
      expect(state.workflows[0]!.enabled).toBe(true);
      expect(state.workflows[0]!.triggers).toBe(2);
      expect(state.workflows[0]!.actions).toBe(5);
      expect(state.workflows[1]!.name).toBe("data-sync");
      expect(state.workflows[1]!.enabled).toBe(false);
      expect(state.workflows[1]!.description).toBeNull();
      expect(state.workflowsLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Network error"); }),
      } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchWorkflows(client);
      const state = useWorkflowsStore.getState();
      expect(state.workflows).toHaveLength(0);
      expect(state.workflowsLoading).toBe(false);
      expect(state.error).toBe("Network error");
    });
  });

  // =========================================================================
  // fetchWorkflowDetail
  // =========================================================================

  describe("fetchWorkflowDetail", () => {
    it("fetches and stores workflow detail by name", async () => {
      const client = mockClient({
        "/api/v2/workflows/deploy-pipeline": {
          name: "deploy-pipeline",
          version: "1.0.0",
          description: "Deploys to production",
          triggers: [{ type: "webhook", config: {} }],
          actions: [{ type: "deploy", config: {} }],
          variables: { env: "production" },
          enabled: true,
        },
      });

      await useWorkflowsStore.getState().fetchWorkflowDetail("deploy-pipeline", client);
      const state = useWorkflowsStore.getState();

      expect(state.selectedWorkflow).not.toBeNull();
      expect(state.selectedWorkflow!.name).toBe("deploy-pipeline");
      expect(state.selectedWorkflow!.version).toBe("1.0.0");
      expect(state.selectedWorkflow!.enabled).toBe(true);
      expect(state.selectedWorkflow!.triggers).toHaveLength(1);
      expect(state.selectedWorkflow!.actions).toHaveLength(1);
      expect(state.selectedWorkflow!.variables).toEqual({ env: "production" });
      expect(state.detailLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Workflow not found"); }),
      } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchWorkflowDetail("missing-wf", client);
      const state = useWorkflowsStore.getState();
      expect(state.selectedWorkflow).toBeNull();
      expect(state.detailLoading).toBe(false);
      expect(state.error).toBe("Workflow not found");
    });
  });

  // =========================================================================
  // executeWorkflow
  // =========================================================================

  describe("executeWorkflow", () => {
    it("triggers execution and refreshes executions list", async () => {
      const executionsList = [
        {
          execution_id: "exec-new",
          workflow_id: "deploy-pipeline",
          trigger_type: "manual",
          status: "running",
          started_at: "2025-01-02T00:00:00Z",
          completed_at: null,
          actions_completed: 0,
          actions_total: 5,
          error_message: null,
        },
        {
          execution_id: "exec-old",
          workflow_id: "deploy-pipeline",
          trigger_type: "cron",
          status: "completed",
          started_at: "2025-01-01T00:00:00Z",
          completed_at: "2025-01-01T00:01:00Z",
          actions_completed: 5,
          actions_total: 5,
          error_message: null,
        },
      ];

      const client = mockClient({
        "/api/v2/workflows/deploy-pipeline/execute": {
          execution_id: "exec-new",
          status: "running",
          actions_completed: 0,
          actions_total: 5,
          error_message: null,
        },
        "/api/v2/workflows/deploy-pipeline/executions": executionsList,
      });

      await useWorkflowsStore.getState().executeWorkflow("deploy-pipeline", client);
      const state = useWorkflowsStore.getState();

      expect(state.executions).toHaveLength(2);
      expect(state.executions[0]!.execution_id).toBe("exec-new");
      expect(state.executions[0]!.status).toBe("running");
      expect(state.executions[1]!.execution_id).toBe("exec-old");
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        post: mock(async () => { throw new Error("Execution denied"); }),
      } as unknown as FetchClient;

      await useWorkflowsStore.getState().executeWorkflow("deploy-pipeline", client);
      const state = useWorkflowsStore.getState();
      expect(state.error).toBe("Execution denied");
    });
  });

  // =========================================================================
  // fetchExecutions
  // =========================================================================

  describe("fetchExecutions", () => {
    it("fetches and stores executions for a workflow by name", async () => {
      const client = mockClient({
        "/api/v2/workflows/deploy-pipeline/executions": [
          {
            execution_id: "exec-1",
            workflow_id: "deploy-pipeline",
            trigger_type: "cron",
            status: "completed",
            started_at: "2025-01-01T00:00:00Z",
            completed_at: "2025-01-01T00:05:00Z",
            actions_completed: 5,
            actions_total: 5,
            error_message: null,
          },
          {
            execution_id: "exec-2",
            workflow_id: "deploy-pipeline",
            trigger_type: "webhook",
            status: "failed",
            started_at: "2025-01-02T00:00:00Z",
            completed_at: "2025-01-02T00:00:30Z",
            actions_completed: 2,
            actions_total: 5,
            error_message: "Timeout exceeded",
          },
        ],
      });

      await useWorkflowsStore.getState().fetchExecutions("deploy-pipeline", client);
      const state = useWorkflowsStore.getState();

      expect(state.executions).toHaveLength(2);
      expect(state.executions[0]!.execution_id).toBe("exec-1");
      expect(state.executions[0]!.status).toBe("completed");
      expect(state.executions[0]!.trigger_type).toBe("cron");
      expect(state.executions[0]!.actions_completed).toBe(5);
      expect(state.executions[0]!.actions_total).toBe(5);
      expect(state.executions[1]!.error_message).toBe("Timeout exceeded");
      expect(state.executions[1]!.actions_completed).toBe(2);
      expect(state.executionsLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("includes limit query param in request path", async () => {
      const getMock = mock(async () => []);
      const client = { get: getMock } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchExecutions("my-workflow", client);

      expect(getMock).toHaveBeenCalledWith(
        "/api/v2/workflows/my-workflow/executions?limit=10",
      );
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Executions unavailable"); }),
      } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchExecutions("deploy-pipeline", client);
      const state = useWorkflowsStore.getState();
      expect(state.executions).toHaveLength(0);
      expect(state.executionsLoading).toBe(false);
      expect(state.error).toBe("Executions unavailable");
    });
  });

  // =========================================================================
  // fetchSchedulerMetrics
  // =========================================================================

  describe("fetchSchedulerMetrics", () => {
    it("fetches and stores scheduler metrics", async () => {
      const client = mockClient({
        "/api/v2/scheduler/metrics": {
          queued_tasks: 10,
          running_tasks: 5,
          completed_tasks: 100,
          failed_tasks: 3,
          avg_wait_ms: 250,
          avg_duration_ms: 5000,
          throughput_per_minute: 12.5,
        },
      });

      await useWorkflowsStore.getState().fetchSchedulerMetrics(client);
      const state = useWorkflowsStore.getState();

      expect(state.schedulerMetrics).not.toBeNull();
      expect(state.schedulerMetrics!.queued_tasks).toBe(10);
      expect(state.schedulerMetrics!.running_tasks).toBe(5);
      expect(state.schedulerMetrics!.completed_tasks).toBe(100);
      expect(state.schedulerMetrics!.failed_tasks).toBe(3);
      expect(state.schedulerMetrics!.avg_wait_ms).toBe(250);
      expect(state.schedulerMetrics!.avg_duration_ms).toBe(5000);
      expect(state.schedulerMetrics!.throughput_per_minute).toBe(12.5);
      expect(state.schedulerLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Scheduler down"); }),
      } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchSchedulerMetrics(client);
      const state = useWorkflowsStore.getState();
      expect(state.schedulerMetrics).toBeNull();
      expect(state.schedulerLoading).toBe(false);
      expect(state.error).toBe("Scheduler down");
    });
  });

  // =========================================================================
  // setSelectedWorkflowIndex
  // =========================================================================

  describe("setSelectedWorkflowIndex", () => {
    it("sets the selected workflow index", () => {
      useWorkflowsStore.getState().setSelectedWorkflowIndex(1);
      expect(useWorkflowsStore.getState().selectedWorkflowIndex).toBe(1);
    });
  });

  // =========================================================================
  // setSelectedExecutionIndex
  // =========================================================================

  describe("setSelectedExecutionIndex", () => {
    it("sets the selected execution index", () => {
      useWorkflowsStore.getState().setSelectedExecutionIndex(3);
      expect(useWorkflowsStore.getState().selectedExecutionIndex).toBe(3);
    });
  });

  // =========================================================================
  // Error handling
  // =========================================================================

  describe("error handling", () => {
    it("clears error when switching tabs", () => {
      useWorkflowsStore.setState({ error: "old error" });
      useWorkflowsStore.getState().setActiveTab("scheduler");
      expect(useWorkflowsStore.getState().error).toBeNull();
    });

    it("fetchWorkflows clears previous error on success", async () => {
      useWorkflowsStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/workflows": [],
      });

      await useWorkflowsStore.getState().fetchWorkflows(client);
      expect(useWorkflowsStore.getState().error).toBeNull();
    });

    it("fetchSchedulerMetrics clears previous error on success", async () => {
      useWorkflowsStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/scheduler/metrics": {
          queued_tasks: 0,
          running_tasks: 0,
          completed_tasks: 0,
          failed_tasks: 0,
          avg_wait_ms: 0,
          avg_duration_ms: 0,
          throughput_per_minute: 0,
        },
      });

      await useWorkflowsStore.getState().fetchSchedulerMetrics(client);
      expect(useWorkflowsStore.getState().error).toBeNull();
    });

    it("handles non-Error thrown objects gracefully", async () => {
      const client = {
        get: mock(async () => { throw "string error"; }),
      } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchWorkflows(client);
      expect(useWorkflowsStore.getState().error).toBe("Failed to fetch workflows");
    });
  });

  // =========================================================================
  // Name-based API paths
  // =========================================================================

  describe("name-based API paths", () => {
    it("uses workflow name in detail URL", async () => {
      const getMock = mock(async () => ({
        name: "my-wf",
        version: "1.0.0",
        description: null,
        triggers: [],
        actions: [],
        variables: {},
        enabled: true,
      }));
      const client = { get: getMock } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchWorkflowDetail("my-wf", client);

      expect(getMock).toHaveBeenCalledWith("/api/v2/workflows/my-wf");
    });

    it("encodes special characters in workflow name", async () => {
      const getMock = mock(async () => ({
        name: "my workflow/v2",
        version: "1.0.0",
        description: null,
        triggers: [],
        actions: [],
        variables: {},
        enabled: true,
      }));
      const client = { get: getMock } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchWorkflowDetail("my workflow/v2", client);

      expect(getMock).toHaveBeenCalledWith(
        `/api/v2/workflows/${encodeURIComponent("my workflow/v2")}`,
      );
    });

    it("uses workflow name in execute URL", async () => {
      const postMock = mock(async () => ({
        execution_id: "exec-1",
        status: "running",
        actions_completed: 0,
        actions_total: 3,
        error_message: null,
      }));
      const getMock = mock(async () => []);
      const client = { post: postMock, get: getMock } as unknown as FetchClient;

      await useWorkflowsStore.getState().executeWorkflow("deploy-pipeline", client);

      expect(postMock).toHaveBeenCalledWith(
        "/api/v2/workflows/deploy-pipeline/execute",
        {},
      );
    });
  });
});
