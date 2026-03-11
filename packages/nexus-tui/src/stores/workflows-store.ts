/**
 * Zustand store for Workflows & Automation panel.
 *
 * Manages workflows, executions, and scheduler metrics
 * across a tabbed interface.
 */

import { create } from "zustand";
import type { FetchClient } from "@nexus/api-client";

// =============================================================================
// Types (snake_case matching API wire format)
// =============================================================================

export interface WorkflowSummary {
  readonly name: string;
  readonly version: string;
  readonly description: string | null;
  readonly enabled: boolean;
  readonly triggers: number;
  readonly actions: number;
}

export interface WorkflowDetail {
  readonly name: string;
  readonly version: string;
  readonly description: string | null;
  readonly triggers: readonly WorkflowTriggerModel[];
  readonly actions: readonly WorkflowActionModel[];
  readonly variables: Readonly<Record<string, unknown>>;
  readonly enabled: boolean;
}

export interface WorkflowTriggerModel {
  readonly [key: string]: unknown;
}

export interface WorkflowActionModel {
  readonly [key: string]: unknown;
}

export interface ExecutionSummary {
  readonly execution_id: string;
  readonly workflow_id: string;
  readonly trigger_type: string;
  readonly status: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;
  readonly actions_completed: number;
  readonly actions_total: number;
  readonly error_message: string | null;
}

export interface ExecutionResult {
  readonly execution_id: string;
  readonly status: string;
  readonly actions_completed: number;
  readonly actions_total: number;
  readonly error_message: string | null;
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

// =============================================================================
// Tab type
// =============================================================================

export type WorkflowTab = "workflows" | "executions" | "scheduler";

// =============================================================================
// Store
// =============================================================================

export interface WorkflowsState {
  // Workflow list
  readonly workflows: readonly WorkflowSummary[];
  readonly selectedWorkflowIndex: number;
  readonly workflowsLoading: boolean;

  // Selected workflow detail
  readonly selectedWorkflow: WorkflowDetail | null;
  readonly detailLoading: boolean;

  // Executions
  readonly executions: readonly ExecutionSummary[];
  readonly selectedExecutionIndex: number;
  readonly executionsLoading: boolean;

  // Scheduler metrics
  readonly schedulerMetrics: SchedulerMetrics | null;
  readonly schedulerLoading: boolean;

  // Tab and error
  readonly activeTab: WorkflowTab;
  readonly error: string | null;

  // Actions
  readonly fetchWorkflows: (client: FetchClient) => Promise<void>;
  readonly fetchWorkflowDetail: (name: string, client: FetchClient) => Promise<void>;
  readonly executeWorkflow: (name: string, client: FetchClient) => Promise<void>;
  readonly fetchExecutions: (workflowName: string, client: FetchClient) => Promise<void>;
  readonly fetchSchedulerMetrics: (client: FetchClient) => Promise<void>;
  readonly setActiveTab: (tab: WorkflowTab) => void;
  readonly setSelectedWorkflowIndex: (index: number) => void;
  readonly setSelectedExecutionIndex: (index: number) => void;
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

  activeTab: "workflows",
  error: null,

  fetchWorkflows: async (client) => {
    set({ workflowsLoading: true, error: null });

    try {
      const workflows = await client.get<readonly WorkflowSummary[]>(
        "/api/v2/workflows",
      );

      set({ workflows: workflows ?? [], workflowsLoading: false });
    } catch (err) {
      set({
        workflowsLoading: false,
        error: err instanceof Error ? err.message : "Failed to fetch workflows",
      });
    }
  },

  fetchWorkflowDetail: async (name, client) => {
    set({ detailLoading: true, error: null });

    try {
      const workflow = await client.get<WorkflowDetail>(
        `/api/v2/workflows/${encodeURIComponent(name)}`,
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

  executeWorkflow: async (name, client) => {
    set({ error: null });

    try {
      await client.post<ExecutionResult>(
        `/api/v2/workflows/${encodeURIComponent(name)}/execute`,
        {},
      );

      // Refresh executions for this workflow after execution
      const { fetchExecutions } = get();
      await fetchExecutions(name, client);
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : "Failed to execute workflow",
      });
    }
  },

  fetchExecutions: async (workflowName, client) => {
    set({ executionsLoading: true, error: null });

    try {
      const executions = await client.get<readonly ExecutionSummary[]>(
        `/api/v2/workflows/${encodeURIComponent(workflowName)}/executions?limit=10`,
      );

      set({ executions: executions ?? [], executionsLoading: false });
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

  setActiveTab: (tab) => {
    set({ activeTab: tab, error: null });
  },

  setSelectedWorkflowIndex: (index) => {
    set({ selectedWorkflowIndex: index });
  },

  setSelectedExecutionIndex: (index) => {
    set({ selectedExecutionIndex: index });
  },
}));
