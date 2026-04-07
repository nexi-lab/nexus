/**
 * Zustand store for Workflows & Automation panel.
 *
 * Manages workflows, executions, and scheduler metrics
 * across a tabbed interface.
 */

import { createStore as create } from "./create-store.js";
import type { FetchClient } from "@nexus-ai-fs/api-client";
import { createApiAction, categorizeError } from "./create-api-action.js";
import { useErrorStore } from "./error-store.js";
import { useUiStore } from "./ui-store.js";

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

export interface ExecutionStep {
  readonly step_index: number;
  readonly action_name: string;
  readonly status: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;
  readonly inputs: Readonly<Record<string, unknown>>;
  readonly outputs: Readonly<Record<string, unknown>>;
  readonly error_message: string | null;
}

export interface ExecutionDetail {
  readonly execution_id: string;
  readonly workflow_id: string;
  readonly trigger_type: string;
  readonly status: string;
  readonly started_at: string | null;
  readonly completed_at: string | null;
  readonly actions_completed: number;
  readonly actions_total: number;
  readonly error_message: string | null;
  readonly steps: readonly ExecutionStep[];
}

export interface SchedulerMetrics {
  readonly queued_tasks: number;
  readonly running_tasks: number;
  readonly completed_tasks: number;
  readonly failed_tasks: number;
  readonly avg_wait_ms: number;
  readonly avg_duration_ms: number;
  readonly throughput_per_minute: number;
  // Raw Astraea API fields
  readonly queue_by_class?: readonly { priority_class: string; count: number }[];
  readonly fair_share?: Record<string, unknown>;
  readonly use_hrrn?: boolean;
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

  // Execution detail (expanded view)
  readonly selectedExecution: ExecutionDetail | null;
  readonly executionDetailLoading: boolean;

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
  readonly createWorkflow: (name: string, description: string, client: FetchClient) => Promise<void>;
  readonly deleteWorkflow: (name: string, client: FetchClient) => Promise<void>;
  readonly enableWorkflow: (name: string, client: FetchClient) => Promise<void>;
  readonly disableWorkflow: (name: string, client: FetchClient) => Promise<void>;
  readonly fetchExecutionDetail: (executionId: string, client: FetchClient) => Promise<void>;
  readonly clearExecutionDetail: () => void;
  readonly setActiveTab: (tab: WorkflowTab) => void;
  readonly setSelectedWorkflowIndex: (index: number) => void;
  readonly setSelectedExecutionIndex: (index: number) => void;
}

const SOURCE = "workflows";

export const useWorkflowsStore = create<WorkflowsState>((set, get) => ({
  workflows: [],
  selectedWorkflowIndex: 0,
  workflowsLoading: false,

  selectedWorkflow: null,
  detailLoading: false,

  executions: [],
  selectedExecutionIndex: 0,
  executionsLoading: false,

  selectedExecution: null,
  executionDetailLoading: false,

  schedulerMetrics: null,
  schedulerLoading: false,

  activeTab: "workflows",
  error: null,

  // =========================================================================
  // Actions migrated to createApiAction (Decision 6A)
  // =========================================================================

  fetchWorkflows: createApiAction<WorkflowsState, [FetchClient]>(set, {
    loadingKey: "workflowsLoading",
    source: SOURCE,
    errorMessage: "Failed to fetch workflows",
    action: async (client) => {
      const workflows = await client.get<readonly WorkflowSummary[]>(
        "/api/v2/workflows",
      );
      return { workflows: workflows ?? [] };
    },
  }),

  fetchWorkflowDetail: async (name, client) => {
    set({ detailLoading: true, error: null });

    try {
      const workflow = await client.get<WorkflowDetail>(
        `/api/v2/workflows/${encodeURIComponent(name)}`,
      );
      set({ selectedWorkflow: workflow, detailLoading: false });
      useUiStore.getState().markDataUpdated("workflows");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch workflow detail";
      set({
        selectedWorkflow: null,
        detailLoading: false,
        error: message,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  // =========================================================================
  // Actions without loading keys — inline with error store integration
  // =========================================================================

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
      const message = err instanceof Error ? err.message : "Failed to execute workflow";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchExecutions: async (workflowName, client) => {
    set({ executionsLoading: true, error: null });

    try {
      const executions = await client.get<readonly ExecutionSummary[]>(
        `/api/v2/workflows/${encodeURIComponent(workflowName)}/executions?limit=10`,
      );

      set({ executions: executions ?? [], executionsLoading: false });
      useUiStore.getState().markDataUpdated("workflows");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch executions";
      set({
        executions: [],
        executionsLoading: false,
        error: message,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchSchedulerMetrics: async (client) => {
    set({ schedulerLoading: true, error: null });

    try {
      const raw = await client.get<Record<string, unknown>>(
        "/api/v2/scheduler/metrics",
      );
      // Normalize Astraea response → SchedulerMetrics
      const queueByClass = (raw.queue_by_class ?? raw.queueByClass ?? []) as readonly { count: number }[];
      const totalQueued = queueByClass.reduce((sum, c) => sum + (c.count ?? 0), 0);
      const metrics: SchedulerMetrics = {
        queued_tasks: totalQueued,
        running_tasks: (raw.running_tasks ?? raw.runningTasks ?? 0) as number,
        completed_tasks: (raw.completed_tasks ?? raw.completedTasks ?? 0) as number,
        failed_tasks: (raw.failed_tasks ?? raw.failedTasks ?? 0) as number,
        avg_wait_ms: (raw.avg_wait_ms ?? raw.avgWaitMs ?? 0) as number,
        avg_duration_ms: (raw.avg_duration_ms ?? raw.avgDurationMs ?? 0) as number,
        throughput_per_minute: (raw.throughput_per_minute ?? raw.throughputPerMinute ?? 0) as number,
        queue_by_class: queueByClass as SchedulerMetrics["queue_by_class"],
        fair_share: (raw.fair_share ?? raw.fairShare ?? {}) as Record<string, unknown>,
        use_hrrn: (raw.use_hrrn ?? raw.useHrrn ?? false) as boolean,
      };
      set({ schedulerMetrics: metrics, schedulerLoading: false });
      useUiStore.getState().markDataUpdated("workflows");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Scheduler metrics unavailable";
      set({ schedulerMetrics: null, schedulerLoading: false });
      // Push to error store for observability but don't set panel-level error
      // (SchedulerView renders a distinct "unavailable" state for null metrics)
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  createWorkflow: async (name, description, client) => {
    set({ error: null });

    try {
      await client.post("/api/v2/workflows", { name, description });
      await get().fetchWorkflows(client);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to create workflow";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  deleteWorkflow: async (name, client) => {
    set({ error: null });

    try {
      await client.delete(`/api/v2/workflows/${encodeURIComponent(name)}`);
      set((state) => ({
        workflows: state.workflows.filter((w) => w.name !== name),
        selectedWorkflowIndex: Math.min(
          state.selectedWorkflowIndex,
          Math.max(state.workflows.length - 2, 0),
        ),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to delete workflow";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  enableWorkflow: async (name, client) => {
    set({ error: null });

    try {
      await client.post(
        `/api/v2/workflows/${encodeURIComponent(name)}/enable`,
        {},
      );
      set((state) => ({
        workflows: state.workflows.map((w) =>
          w.name === name ? { ...w, enabled: true } : w,
        ),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to enable workflow";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  disableWorkflow: async (name, client) => {
    set({ error: null });

    try {
      await client.post(
        `/api/v2/workflows/${encodeURIComponent(name)}/disable`,
        {},
      );
      set((state) => ({
        workflows: state.workflows.map((w) =>
          w.name === name ? { ...w, enabled: false } : w,
        ),
      }));
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to disable workflow";
      set({ error: message });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  fetchExecutionDetail: async (executionId, client) => {
    set({ executionDetailLoading: true, error: null });

    try {
      const detail = await client.get<ExecutionDetail>(
        `/api/v2/workflows/executions/${encodeURIComponent(executionId)}`,
      );
      set({ selectedExecution: detail, executionDetailLoading: false });
      useUiStore.getState().markDataUpdated("workflows");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to fetch execution detail";
      set({
        selectedExecution: null,
        executionDetailLoading: false,
        error: message,
      });
      useErrorStore.getState().pushError({ message, category: categorizeError(message), source: SOURCE });
    }
  },

  clearExecutionDetail: () => {
    set({ selectedExecution: null });
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
