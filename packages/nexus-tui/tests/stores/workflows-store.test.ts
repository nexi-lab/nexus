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
    trajectories: [],
    selectedTrajectoryIndex: 0,
    trajectoriesLoading: false,
    selectedTrajectory: null,
    trajectoryDetailLoading: false,
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

      useWorkflowsStore.getState().setActiveTab("trajectories");
      expect(useWorkflowsStore.getState().activeTab).toBe("trajectories");

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
    it("fetches and stores workflows", async () => {
      const client = mockClient({
        "/api/v2/workflows": {
          workflows: [
            {
              workflow_id: "wf-1",
              name: "Deploy pipeline",
              description: "Deploys to production",
              status: "active",
              trigger_type: "webhook",
              step_count: 5,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-02T00:00:00Z",
              last_run: "2025-01-02T12:00:00Z",
            },
            {
              workflow_id: "wf-2",
              name: "Data sync",
              description: "Syncs data",
              status: "paused",
              trigger_type: "cron",
              step_count: 3,
              created_at: "2025-01-01T00:00:00Z",
              updated_at: "2025-01-01T00:00:00Z",
              last_run: null,
            },
          ],
          total: 2,
        },
      });

      await useWorkflowsStore.getState().fetchWorkflows(client);
      const state = useWorkflowsStore.getState();

      expect(state.workflows).toHaveLength(2);
      expect(state.workflows[0]!.workflow_id).toBe("wf-1");
      expect(state.workflows[0]!.name).toBe("Deploy pipeline");
      expect(state.workflows[0]!.status).toBe("active");
      expect(state.workflows[1]!.trigger_type).toBe("cron");
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
    it("fetches and stores a single workflow", async () => {
      const client = mockClient({
        "/api/v2/workflows/wf-1": {
          workflow_id: "wf-1",
          name: "Deploy pipeline",
          description: "Deploys to production",
          status: "active",
          trigger_type: "webhook",
          step_count: 5,
          created_at: "2025-01-01T00:00:00Z",
          updated_at: "2025-01-02T00:00:00Z",
          last_run: "2025-01-02T12:00:00Z",
        },
      });

      await useWorkflowsStore.getState().fetchWorkflowDetail("wf-1", client);
      const state = useWorkflowsStore.getState();

      expect(state.selectedWorkflow).not.toBeNull();
      expect(state.selectedWorkflow!.workflow_id).toBe("wf-1");
      expect(state.selectedWorkflow!.name).toBe("Deploy pipeline");
      expect(state.detailLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Workflow not found"); }),
      } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchWorkflowDetail("wf-missing", client);
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
    it("triggers execution and prepends to executions list", async () => {
      useWorkflowsStore.setState({
        executions: [
          {
            execution_id: "exec-old",
            workflow_id: "wf-1",
            status: "completed",
            started_at: "2025-01-01T00:00:00Z",
            completed_at: "2025-01-01T00:01:00Z",
            duration_ms: 60000,
            trigger: "manual",
            error: null,
          },
        ],
      });

      const client = mockClient({
        "/api/v2/workflows/wf-1/execute": {
          execution_id: "exec-new",
          workflow_id: "wf-1",
          status: "running",
          started_at: "2025-01-02T00:00:00Z",
          completed_at: null,
          duration_ms: null,
          trigger: "manual",
          error: null,
        },
      });

      await useWorkflowsStore.getState().executeWorkflow("wf-1", client);
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

      await useWorkflowsStore.getState().executeWorkflow("wf-1", client);
      const state = useWorkflowsStore.getState();
      expect(state.error).toBe("Execution denied");
    });
  });

  // =========================================================================
  // fetchExecutions
  // =========================================================================

  describe("fetchExecutions", () => {
    it("fetches and stores executions for a workflow", async () => {
      const client = mockClient({
        "/api/v2/workflows/wf-1/executions": {
          executions: [
            {
              execution_id: "exec-1",
              workflow_id: "wf-1",
              status: "completed",
              started_at: "2025-01-01T00:00:00Z",
              completed_at: "2025-01-01T00:05:00Z",
              duration_ms: 300000,
              trigger: "cron",
              error: null,
            },
            {
              execution_id: "exec-2",
              workflow_id: "wf-1",
              status: "failed",
              started_at: "2025-01-02T00:00:00Z",
              completed_at: "2025-01-02T00:00:30Z",
              duration_ms: 30000,
              trigger: "webhook",
              error: "Timeout exceeded",
            },
          ],
        },
      });

      await useWorkflowsStore.getState().fetchExecutions("wf-1", client);
      const state = useWorkflowsStore.getState();

      expect(state.executions).toHaveLength(2);
      expect(state.executions[0]!.execution_id).toBe("exec-1");
      expect(state.executions[0]!.status).toBe("completed");
      expect(state.executions[1]!.error).toBe("Timeout exceeded");
      expect(state.executionsLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Executions unavailable"); }),
      } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchExecutions("wf-1", client);
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
  // fetchTrajectories
  // =========================================================================

  describe("fetchTrajectories", () => {
    it("fetches and stores trajectories", async () => {
      const client = mockClient({
        "/api/v2/trajectories": {
          trajectories: [
            {
              trajectory_id: "traj-1",
              agent_id: "agent-1",
              status: "completed",
              step_count: 4,
              started_at: "2025-01-01T00:00:00Z",
              completed_at: "2025-01-01T00:10:00Z",
              steps: [],
            },
            {
              trajectory_id: "traj-2",
              agent_id: "agent-2",
              status: "active",
              step_count: 2,
              started_at: "2025-01-02T00:00:00Z",
              completed_at: null,
              steps: [],
            },
          ],
        },
      });

      await useWorkflowsStore.getState().fetchTrajectories(client);
      const state = useWorkflowsStore.getState();

      expect(state.trajectories).toHaveLength(2);
      expect(state.trajectories[0]!.trajectory_id).toBe("traj-1");
      expect(state.trajectories[0]!.status).toBe("completed");
      expect(state.trajectories[1]!.agent_id).toBe("agent-2");
      expect(state.trajectoriesLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Trajectories unavailable"); }),
      } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchTrajectories(client);
      const state = useWorkflowsStore.getState();
      expect(state.trajectories).toHaveLength(0);
      expect(state.trajectoriesLoading).toBe(false);
      expect(state.error).toBe("Trajectories unavailable");
    });
  });

  // =========================================================================
  // fetchTrajectoryDetail
  // =========================================================================

  describe("fetchTrajectoryDetail", () => {
    it("fetches and stores trajectory with steps", async () => {
      const client = mockClient({
        "/api/v2/trajectories/traj-1": {
          trajectory_id: "traj-1",
          agent_id: "agent-1",
          status: "completed",
          step_count: 2,
          started_at: "2025-01-01T00:00:00Z",
          completed_at: "2025-01-01T00:10:00Z",
          steps: [
            {
              step_id: "step-1",
              action: "read_file",
              status: "completed",
              started_at: "2025-01-01T00:00:00Z",
              duration_ms: 150,
              output: "file content",
            },
            {
              step_id: "step-2",
              action: "write_file",
              status: "completed",
              started_at: "2025-01-01T00:01:00Z",
              duration_ms: 200,
              output: null,
            },
          ],
        },
      });

      await useWorkflowsStore.getState().fetchTrajectoryDetail("traj-1", client);
      const state = useWorkflowsStore.getState();

      expect(state.selectedTrajectory).not.toBeNull();
      expect(state.selectedTrajectory!.trajectory_id).toBe("traj-1");
      expect(state.selectedTrajectory!.steps).toHaveLength(2);
      expect(state.selectedTrajectory!.steps[0]!.action).toBe("read_file");
      expect(state.selectedTrajectory!.steps[0]!.duration_ms).toBe(150);
      expect(state.selectedTrajectory!.steps[1]!.output).toBeNull();
      expect(state.trajectoryDetailLoading).toBe(false);
      expect(state.error).toBeNull();
    });

    it("sets error on failure", async () => {
      const client = {
        get: mock(async () => { throw new Error("Trajectory not found"); }),
      } as unknown as FetchClient;

      await useWorkflowsStore.getState().fetchTrajectoryDetail("traj-missing", client);
      const state = useWorkflowsStore.getState();
      expect(state.selectedTrajectory).toBeNull();
      expect(state.trajectoryDetailLoading).toBe(false);
      expect(state.error).toBe("Trajectory not found");
    });
  });

  // =========================================================================
  // setSelectedWorkflowIndex
  // =========================================================================

  describe("setSelectedWorkflowIndex", () => {
    it("sets the index and updates selectedWorkflow", () => {
      useWorkflowsStore.setState({
        workflows: [
          {
            workflow_id: "wf-1",
            name: "First",
            description: "",
            status: "active",
            trigger_type: "manual",
            step_count: 1,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
            last_run: null,
          },
          {
            workflow_id: "wf-2",
            name: "Second",
            description: "",
            status: "paused",
            trigger_type: "cron",
            step_count: 2,
            created_at: "2025-01-01T00:00:00Z",
            updated_at: "2025-01-01T00:00:00Z",
            last_run: null,
          },
        ],
      });

      useWorkflowsStore.getState().setSelectedWorkflowIndex(1);
      const state = useWorkflowsStore.getState();
      expect(state.selectedWorkflowIndex).toBe(1);
      expect(state.selectedWorkflow).not.toBeNull();
      expect(state.selectedWorkflow!.workflow_id).toBe("wf-2");
    });

    it("sets selectedWorkflow to null for out-of-bounds index", () => {
      useWorkflowsStore.setState({ workflows: [] });
      useWorkflowsStore.getState().setSelectedWorkflowIndex(5);
      expect(useWorkflowsStore.getState().selectedWorkflow).toBeNull();
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
  // setSelectedTrajectoryIndex
  // =========================================================================

  describe("setSelectedTrajectoryIndex", () => {
    it("sets the selected trajectory index", () => {
      useWorkflowsStore.getState().setSelectedTrajectoryIndex(7);
      expect(useWorkflowsStore.getState().selectedTrajectoryIndex).toBe(7);
    });
  });

  // =========================================================================
  // Error handling
  // =========================================================================

  describe("error handling", () => {
    it("clears error when switching tabs", () => {
      useWorkflowsStore.setState({ error: "old error" });
      useWorkflowsStore.getState().setActiveTab("trajectories");
      expect(useWorkflowsStore.getState().error).toBeNull();
    });

    it("fetchWorkflows clears previous error on success", async () => {
      useWorkflowsStore.setState({ error: "stale error" });

      const client = mockClient({
        "/api/v2/workflows": { workflows: [], total: 0 },
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
});
