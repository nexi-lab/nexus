/**
 * Workflows & Automation panel: tabbed layout with workflows, executions,
 * scheduler metrics, and trajectory views.
 */

import React, { useEffect } from "react";
import { useWorkflowsStore } from "../../stores/workflows-store.js";
import type { WorkflowTab } from "../../stores/workflows-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { WorkflowList } from "./workflow-list.js";
import { ExecutionList } from "./execution-list.js";
import { SchedulerView } from "./scheduler-view.js";
import { TrajectoryView } from "./trajectory-view.js";

const TAB_ORDER: readonly WorkflowTab[] = [
  "workflows",
  "executions",
  "scheduler",
  "trajectories",
];

const TAB_LABELS: Readonly<Record<WorkflowTab, string>> = {
  workflows: "Workflows",
  executions: "Executions",
  scheduler: "Scheduler",
  trajectories: "Trajectories",
};

export default function WorkflowsPanel(): React.ReactNode {
  const client = useApi();

  const workflows = useWorkflowsStore((s) => s.workflows);
  const selectedWorkflowIndex = useWorkflowsStore((s) => s.selectedWorkflowIndex);
  const workflowsLoading = useWorkflowsStore((s) => s.workflowsLoading);
  const selectedWorkflow = useWorkflowsStore((s) => s.selectedWorkflow);
  const detailLoading = useWorkflowsStore((s) => s.detailLoading);
  const executions = useWorkflowsStore((s) => s.executions);
  const selectedExecutionIndex = useWorkflowsStore((s) => s.selectedExecutionIndex);
  const executionsLoading = useWorkflowsStore((s) => s.executionsLoading);
  const schedulerMetrics = useWorkflowsStore((s) => s.schedulerMetrics);
  const schedulerLoading = useWorkflowsStore((s) => s.schedulerLoading);
  const trajectories = useWorkflowsStore((s) => s.trajectories);
  const selectedTrajectoryIndex = useWorkflowsStore((s) => s.selectedTrajectoryIndex);
  const trajectoriesLoading = useWorkflowsStore((s) => s.trajectoriesLoading);
  const selectedTrajectory = useWorkflowsStore((s) => s.selectedTrajectory);
  const trajectoryDetailLoading = useWorkflowsStore((s) => s.trajectoryDetailLoading);
  const activeTab = useWorkflowsStore((s) => s.activeTab);
  const error = useWorkflowsStore((s) => s.error);

  const fetchWorkflows = useWorkflowsStore((s) => s.fetchWorkflows);
  const fetchWorkflowDetail = useWorkflowsStore((s) => s.fetchWorkflowDetail);
  const executeWorkflow = useWorkflowsStore((s) => s.executeWorkflow);
  const fetchExecutions = useWorkflowsStore((s) => s.fetchExecutions);
  const fetchSchedulerMetrics = useWorkflowsStore((s) => s.fetchSchedulerMetrics);
  const fetchTrajectories = useWorkflowsStore((s) => s.fetchTrajectories);
  const fetchTrajectoryDetail = useWorkflowsStore((s) => s.fetchTrajectoryDetail);
  const setActiveTab = useWorkflowsStore((s) => s.setActiveTab);
  const setSelectedWorkflowIndex = useWorkflowsStore((s) => s.setSelectedWorkflowIndex);
  const setSelectedExecutionIndex = useWorkflowsStore((s) => s.setSelectedExecutionIndex);
  const setSelectedTrajectoryIndex = useWorkflowsStore((s) => s.setSelectedTrajectoryIndex);

  // Refresh current view based on active tab
  const refreshCurrentView = (): void => {
    if (!client) return;

    if (activeTab === "workflows") {
      fetchWorkflows(client);
    } else if (activeTab === "executions") {
      const wf = workflows[selectedWorkflowIndex];
      if (wf) {
        fetchExecutions(wf.workflow_id, client);
      }
    } else if (activeTab === "scheduler") {
      fetchSchedulerMetrics(client);
    } else if (activeTab === "trajectories") {
      fetchTrajectories(client);
    }
  };

  // Auto-fetch when tab changes
  useEffect(() => {
    refreshCurrentView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, client]);

  // Resolve the list length for current tab navigation
  const currentListLength = (): number => {
    if (activeTab === "workflows") return workflows.length;
    if (activeTab === "executions") return executions.length;
    if (activeTab === "trajectories") return trajectories.length;
    return 0;
  };

  const currentIndex = (): number => {
    if (activeTab === "workflows") return selectedWorkflowIndex;
    if (activeTab === "executions") return selectedExecutionIndex;
    if (activeTab === "trajectories") return selectedTrajectoryIndex;
    return 0;
  };

  const setCurrentIndex = (index: number): void => {
    if (activeTab === "workflows") {
      setSelectedWorkflowIndex(index);
    } else if (activeTab === "executions") {
      setSelectedExecutionIndex(index);
    } else if (activeTab === "trajectories") {
      setSelectedTrajectoryIndex(index);
    }
  };

  useKeyboard({
    j: () => {
      const maxIndex = currentListLength() - 1;
      if (maxIndex >= 0) {
        setCurrentIndex(Math.min(currentIndex() + 1, maxIndex));
      }
    },
    down: () => {
      const maxIndex = currentListLength() - 1;
      if (maxIndex >= 0) {
        setCurrentIndex(Math.min(currentIndex() + 1, maxIndex));
      }
    },
    k: () => {
      setCurrentIndex(Math.max(currentIndex() - 1, 0));
    },
    up: () => {
      setCurrentIndex(Math.max(currentIndex() - 1, 0));
    },
    tab: () => {
      const currentIdx = TAB_ORDER.indexOf(activeTab);
      const nextIdx = (currentIdx + 1) % TAB_ORDER.length;
      const nextTab = TAB_ORDER[nextIdx];
      if (nextTab) {
        setActiveTab(nextTab);
      }
    },
    r: () => refreshCurrentView(),
    e: () => {
      if (activeTab !== "workflows" || !client) return;
      const wf = workflows[selectedWorkflowIndex];
      if (wf && wf.status === "active") {
        executeWorkflow(wf.workflow_id, client);
      }
    },
    return: () => {
      if (!client) return;

      if (activeTab === "workflows") {
        const wf = workflows[selectedWorkflowIndex];
        if (wf) {
          fetchWorkflowDetail(wf.workflow_id, client);
        }
      } else if (activeTab === "trajectories") {
        const traj = trajectories[selectedTrajectoryIndex];
        if (traj) {
          fetchTrajectoryDetail(traj.trajectory_id, client);
        }
      }
    },
  });

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Tab bar */}
      <box height={1} width="100%">
        <text>
          {TAB_ORDER.map((tab) => {
            const label = TAB_LABELS[tab];
            return tab === activeTab ? `[${label}]` : ` ${label} `;
          }).join(" ")}
        </text>
      </box>

      {/* Error display */}
      {error && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {/* Detail content */}
      <box flexGrow={1} borderStyle="single">
        {activeTab === "workflows" && (
          <WorkflowList
            workflows={workflows}
            selectedIndex={selectedWorkflowIndex}
            loading={workflowsLoading}
          />
        )}
        {activeTab === "executions" && (
          <ExecutionList
            executions={executions}
            selectedIndex={selectedExecutionIndex}
            loading={executionsLoading}
          />
        )}
        {activeTab === "scheduler" && (
          <SchedulerView
            metrics={schedulerMetrics}
            loading={schedulerLoading}
          />
        )}
        {activeTab === "trajectories" && (
          <TrajectoryView
            trajectories={trajectories}
            selectedIndex={selectedTrajectoryIndex}
            selectedTrajectory={selectedTrajectory}
            loading={trajectoriesLoading}
            detailLoading={trajectoryDetailLoading}
          />
        )}
      </box>

      {/* Workflow detail overlay when loaded */}
      {selectedWorkflow && activeTab === "workflows" && !detailLoading && (
        <box height={3} width="100%">
          <text>
            {`Detail: ${selectedWorkflow.name} | ${selectedWorkflow.status} | ${selectedWorkflow.step_count} steps | trigger: ${selectedWorkflow.trigger_type}`}
          </text>
        </box>
      )}

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {"j/k:navigate  Tab:switch tab  e:execute  r:refresh  Enter:detail  q:quit"}
        </text>
      </box>
    </box>
  );
}
