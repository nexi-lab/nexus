/**
 * Zustand store for Workflows & Automation panel.
 *
 * Manages workflows, executions, scheduler metrics, and trajectories
 * across a tabbed interface.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

// =============================================================================
// Types (snake_case matching API wire format)
// =============================================================================

export interface Workflow {
  readonly workflow_id: string;
  readonly name: string;
  readonly description: string;
  readonly status: "active" | "paused" | "draft" | "archived";
  readonly trigger_type: string;
  readonly step_count: number;
  readonly created_at: string;
  readonly updated_at: string;
  readonly last_run: string | null;
}

export interface Execution {
  readonly execution_id: string;
  readonly workflow_id: string;
  readonly status: "running" | "completed" | "failed" | "cancelled";
  readonly started_at: string;
  readonly completed_at: string | null;
  readonly duration_ms: number | null;
  readonly trigger: string;
  readonly error: string | null;
}

export interface SchedulerMetrics {
  readonly queued_tasks: number;
  readonly running_tasks: number;
  readonly completed_tasks: number;
  readonly failed_tasks: number;
  readonly avg_wait_ms: number;
  readonly avg_duration_ms: number;
  readonly throughput_per_minute: number;
}

export interface TrajectoryStep {
  readonly step_id: string;
  readonly action: string;
  readonly status: "completed" | "failed" | "skipped";
  readonly started_at: string;
  readonly duration_ms: number;
  readonly output: string | null;
}

export interface Trajectory {
  readonly trajectory_id: string;
  readonly agent_id: string;
  readonly status: "active" | "completed" | "failed";
  readonly step_count: number;
  readonly started_at: string;
  readonly completed_at: string | null;
  readonly steps: readonly TrajectoryStep[];
}

interface WorkflowListResponse {
  readonly workflows: readonly Workflow[];
  readonly total: number;
}

interface ExecutionListResponse {
  readonly executions: readonly Execution[];
}

interface TrajectoryListResponse {
  readonly trajectories: readonly Trajectory[];
}

// =============================================================================
// Tab type
// =============================================================================

export type WorkflowTab = "workflows" | "executions" | "scheduler" | "trajectories";

// =============================================================================
// Store
// =============================================================================

export interface WorkflowsState {
  // Workflow list
  readonly workflows: readonly Workflow[];
  readonly selectedWorkflowIndex: number;
  readonly workflowsLoading: boolean;

  // Selected workflow detail
  readonly selectedWorkflow: Workflow | null;
  readonly detailLoading: boolean;

  // Executions
  readonly executions: readonly Execution[];
  readonly selectedExecutionIndex: number;
  readonly executionsLoading: boolean;

  // Scheduler metrics
  readonly schedulerMetrics: SchedulerMetrics | null;
  readonly schedulerLoading: boolean;

  // Trajectories
  readonly trajectories: readonly Trajectory[];
  readonly selectedTrajectoryIndex: number;
  readonly trajectoriesLoading: boolean;
  readonly selectedTrajectory: Trajectory | null;
  readonly trajectoryDetailLoading: boolean;

  // Tab and error
  readonly activeTab: WorkflowTab;
  readonly error: string | null;

  // Actions
  readonly fetchWorkflows: (client: FetchClient) => Promise<void>;
  readonly fetchWorkflowDetail: (id: string, client: FetchClient) => Promise<void>;
  readonly executeWorkflow: (id: string, client: FetchClient) => Promise<void>;
  readonly fetchExecutions: (workflowId: string, client: FetchClient) => Promise<void>;
  readonly fetchSchedulerMetrics: (client: FetchClient) => Promise<void>;
  readonly fetchTrajectories: (client: FetchClient) => Promise<void>;
  readonly fetchTrajectoryDetail: (id: string, client: FetchClient) => Promise<void>;
  readonly setActiveTab: (tab: WorkflowTab) => void;
  readonly setSelectedWorkflowIndex: (index: number) => void;
  readonly setSelectedExecutionIndex: (index: number) => void;
  readonly setSelectedTrajectoryIndex: (index: number) => void;
}

export const useWorkflowsStore = create<WorkflowsState>((set, get) => ({
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

  fetchWorkflows: async (client) => {
    set({ workflowsLoading: true, error: null });

    try {
      const response = await client.get<WorkflowListResponse>(
        "/api/v2/workflows",
      );

      const workflows = response.workflows ?? [];
      set({ workflows, workflowsLoading: false });
    } catch (err) {
      set({
        workflowsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch workflows",
      });
    }
  },

  fetchWorkflowDetail: async (id, client) => {
    set({ detailLoading: true, error: null });

    try {
      const workflow = await client.get<Workflow>(
        `/api/v2/workflows/${encodeURIComponent(id)}`,
      );
      set({ selectedWorkflow: workflow, detailLoading: false });
    } catch (err) {
      set({
        selectedWorkflow: null,
        detailLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch workflow detail",
      });
    }
  },

  executeWorkflow: async (id, client) => {
    set({ error: null });

    try {
      const execution = await client.post<Execution>(
        `/api/v2/workflows/${encodeURIComponent(id)}/execute`,
        {},
      );

      // Add the new execution to the front of the list
      const { executions } = get();
      set({ executions: [execution, ...executions] });
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to execute workflow",
      });
    }
  },

  fetchExecutions: async (workflowId, client) => {
    set({ executionsLoading: true, error: null });

    try {
      const response = await client.get<ExecutionListResponse>(
        `/api/v2/workflows/${encodeURIComponent(workflowId)}/executions`,
      );

      const executions = response.executions ?? [];
      set({ executions, executionsLoading: false });
    } catch (err) {
      set({
        executions: [],
        executionsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch executions",
      });
    }
  },

  fetchSchedulerMetrics: async (client) => {
    set({ schedulerLoading: true, error: null });

    try {
      const metrics = await client.get<SchedulerMetrics>(
        "/api/v2/scheduler/metrics",
      );
      set({ schedulerMetrics: metrics, schedulerLoading: false });
    } catch (err) {
      set({
        schedulerMetrics: null,
        schedulerLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch scheduler metrics",
      });
    }
  },

  fetchTrajectories: async (client) => {
    set({ trajectoriesLoading: true, error: null });

    try {
      const response = await client.get<TrajectoryListResponse>(
        "/api/v2/trajectories",
      );

      const trajectories = response.trajectories ?? [];
      set({ trajectories, trajectoriesLoading: false });
    } catch (err) {
      set({
        trajectories: [],
        trajectoriesLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch trajectories",
      });
    }
  },

  fetchTrajectoryDetail: async (id, client) => {
    set({ trajectoryDetailLoading: true, error: null });

    try {
      const trajectory = await client.get<Trajectory>(
        `/api/v2/trajectories/${encodeURIComponent(id)}`,
      );
      set({ selectedTrajectory: trajectory, trajectoryDetailLoading: false });
    } catch (err) {
      set({
        selectedTrajectory: null,
        trajectoryDetailLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch trajectory detail",
      });
    }
  },

  setActiveTab: (tab) => {
    set({ activeTab: tab, error: null });
  },

  setSelectedWorkflowIndex: (index) => {
    const { workflows } = get();
    const workflow = workflows[index] ?? null;
    set({ selectedWorkflowIndex: index, selectedWorkflow: workflow });
  },

  setSelectedExecutionIndex: (index) => {
    set({ selectedExecutionIndex: index });
  },

  setSelectedTrajectoryIndex: (index) => {
    set({ selectedTrajectoryIndex: index });
  },
}));
