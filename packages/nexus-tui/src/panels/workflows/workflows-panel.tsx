/**
 * Workflows & Automation panel: tabbed layout with workflows, executions,
 * and scheduler metrics views.
 */

import React, { useEffect, useState, useCallback } from "react";
import { useWorkflowsStore } from "../../stores/workflows-store.js";
import type { WorkflowTab } from "../../stores/workflows-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { jumpToStart, jumpToEnd } from "../../shared/hooks/use-list-navigation.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useUiStore } from "../../stores/ui-store.js";
import { BrickGate } from "../../shared/components/brick-gate.js";
import { ConfirmDialog } from "../../shared/components/confirm-dialog.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { WorkflowList } from "./workflow-list.js";
import { ExecutionList } from "./execution-list.js";
import { SchedulerView } from "./scheduler-view.js";
import { Tooltip } from "../../shared/components/tooltip.js";

const TAB_ORDER: readonly WorkflowTab[] = [
  "workflows",
  "executions",
  "scheduler",
];

const TAB_LABELS: Readonly<Record<WorkflowTab, string>> = {
  workflows: "Workflows",
  executions: "Executions",
  scheduler: "Scheduler",
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
  const selectedExecution = useWorkflowsStore((s) => s.selectedExecution);
  const executionDetailLoading = useWorkflowsStore((s) => s.executionDetailLoading);
  const schedulerMetrics = useWorkflowsStore((s) => s.schedulerMetrics);
  const schedulerLoading = useWorkflowsStore((s) => s.schedulerLoading);
  const activeTab = useWorkflowsStore((s) => s.activeTab);
  const error = useWorkflowsStore((s) => s.error);

  const fetchWorkflows = useWorkflowsStore((s) => s.fetchWorkflows);
  const fetchWorkflowDetail = useWorkflowsStore((s) => s.fetchWorkflowDetail);
  const executeWorkflow = useWorkflowsStore((s) => s.executeWorkflow);
  const fetchExecutions = useWorkflowsStore((s) => s.fetchExecutions);
  const fetchSchedulerMetrics = useWorkflowsStore((s) => s.fetchSchedulerMetrics);
  const deleteWorkflow = useWorkflowsStore((s) => s.deleteWorkflow);
  const enableWorkflow = useWorkflowsStore((s) => s.enableWorkflow);
  const disableWorkflow = useWorkflowsStore((s) => s.disableWorkflow);
  const fetchExecutionDetail = useWorkflowsStore((s) => s.fetchExecutionDetail);
  const clearExecutionDetail = useWorkflowsStore((s) => s.clearExecutionDetail);
  const setActiveTab = useWorkflowsStore((s) => s.setActiveTab);
  const setSelectedWorkflowIndex = useWorkflowsStore((s) => s.setSelectedWorkflowIndex);
  const setSelectedExecutionIndex = useWorkflowsStore((s) => s.setSelectedExecutionIndex);

  const overlayActive = useUiStore((s) => s.overlayActive);

  // Track in-flight workflow execution
  const [executing, setExecuting] = useState(false);

  // Confirmation dialog state for destructive delete action
  const [confirmDelete, setConfirmDelete] = useState(false);

  const handleConfirmDelete = useCallback(() => {
    if (!client) return;
    const wf = workflows[selectedWorkflowIndex];
    if (wf) {
      deleteWorkflow(wf.name, client);
    }
    setConfirmDelete(false);
  }, [client, workflows, selectedWorkflowIndex, deleteWorkflow]);

  const handleCancelDelete = useCallback(() => {
    setConfirmDelete(false);
  }, []);

  // Refresh current view based on active tab
  const refreshCurrentView = (): void => {
    if (!client) return;

    if (activeTab === "workflows") {
      fetchWorkflows(client);
    } else if (activeTab === "executions") {
      const wf = workflows[selectedWorkflowIndex];
      if (wf) {
        fetchExecutions(wf.name, client);
      }
    } else if (activeTab === "scheduler") {
      fetchSchedulerMetrics(client);
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
    return 0;
  };

  const currentIndex = (): number => {
    if (activeTab === "workflows") return selectedWorkflowIndex;
    if (activeTab === "executions") return selectedExecutionIndex;
    return 0;
  };

  const setCurrentIndex = (index: number): void => {
    if (activeTab === "workflows") {
      setSelectedWorkflowIndex(index);
    } else if (activeTab === "executions") {
      setSelectedExecutionIndex(index);
    }
  };

  useKeyboard(
    overlayActive
      ? {}
      : confirmDelete
      ? {} // ConfirmDialog handles its own keys when visible
      : {
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
            if (wf && wf.enabled) {
              setExecuting(true);
              executeWorkflow(wf.name, client).finally(() => setExecuting(false));
            }
          },
          d: () => {
            if (activeTab !== "workflows") return;
            const wf = workflows[selectedWorkflowIndex];
            if (wf) setConfirmDelete(true);
          },
          p: () => {
            if (activeTab !== "workflows" || !client) return;
            const wf = workflows[selectedWorkflowIndex];
            if (wf) {
              if (wf.enabled) disableWorkflow(wf.name, client);
              else enableWorkflow(wf.name, client);
            }
          },
          return: () => {
            if (!client) return;

            if (activeTab === "workflows") {
              const wf = workflows[selectedWorkflowIndex];
              if (wf) {
                fetchWorkflowDetail(wf.name, client);
              }
            } else if (activeTab === "executions") {
              const ex = executions[selectedExecutionIndex];
              if (ex) {
                // Toggle: if detail is shown for this execution, clear it
                if (selectedExecution?.execution_id === ex.execution_id) {
                  clearExecutionDetail();
                } else {
                  fetchExecutionDetail(ex.execution_id, client);
                }
              }
            }
          },
          escape: () => {
            // Clear expanded detail views
            if (activeTab === "executions" && selectedExecution) {
              clearExecutionDetail();
            }
          },
          g: () => {
            setCurrentIndex(jumpToStart());
          },
          "shift+g": () => {
            setCurrentIndex(jumpToEnd(currentListLength()));
          },
        },
  );

  return (
    <BrickGate brick={["workflows", "scheduler"]}>
      <box height="100%" width="100%" flexDirection="column">
        <Tooltip tooltipKey="workflows-panel" message="Tip: Press ? for keybinding help" />
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

        {/* Execution in-flight indicator */}
        {executing && (
          <box height={1} width="100%">
            <LoadingIndicator message="Executing workflow..." centered={false} />
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
        </box>

        {/* Execution detail overlay when loaded */}
        {activeTab === "executions" && executionDetailLoading && (
          <box height={1} width="100%">
            <LoadingIndicator message="Loading execution detail..." centered={false} />
          </box>
        )}
        {activeTab === "executions" && selectedExecution && !executionDetailLoading && (
          <box height={Math.min((selectedExecution.steps?.length ?? 0) + 3, 12)} width="100%" borderStyle="single" flexDirection="column">
            <text>
              {`Execution: ${selectedExecution.execution_id} | ${selectedExecution.status} | ${selectedExecution.actions_completed}/${selectedExecution.actions_total} actions`}
            </text>
            {(selectedExecution.steps ?? []).length > 0 ? (
              <scrollbox flexGrow={1} width="100%">
                {(selectedExecution.steps ?? []).map((step, i) => (
                  <box key={i} height={1} width="100%">
                    <text>
                      {`  ${String(step.step_index).padEnd(3)} ${(step.action_name ?? "").padEnd(20)} ${step.status.padEnd(10)} ${step.error_message ? `ERR: ${step.error_message}` : ""}`}
                    </text>
                  </box>
                ))}
              </scrollbox>
            ) : (
              <text dimColor>  No steps recorded</text>
            )}
          </box>
        )}

        {/* Workflow detail overlay when loaded */}
        {selectedWorkflow && activeTab === "workflows" && !detailLoading && (
          <box height={3} width="100%">
            <text>
              {`Detail: ${selectedWorkflow.name} | v${selectedWorkflow.version} | ${selectedWorkflow.enabled ? "enabled" : "disabled"} | ${selectedWorkflow.triggers.length} triggers | ${selectedWorkflow.actions.length} actions`}
            </text>
          </box>
        )}

        {/* Help bar */}
        <box height={1} width="100%">
          <text>
            {activeTab === "executions"
              ? "j/k:navigate  Tab:switch tab  Enter:detail  Esc:close  r:refresh  q:quit"
              : "j/k:navigate  Tab:switch tab  e:execute  d:delete  p:enable/disable  r:refresh  Enter:detail  q:quit"}
          </text>
        </box>

        {/* Delete confirmation dialog */}
        <ConfirmDialog
          visible={confirmDelete}
          title="Delete Workflow"
          message={`Permanently delete "${workflows[selectedWorkflowIndex]?.name ?? ""}"? This cannot be undone.`}
          onConfirm={handleConfirmDelete}
          onCancel={handleCancelDelete}
        />
      </box>
    </BrickGate>
  );
}
