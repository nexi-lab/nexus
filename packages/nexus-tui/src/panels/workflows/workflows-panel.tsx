/**
 * Workflows & Automation panel: tabbed layout with workflows, executions,
 * and scheduler metrics views.
 */

import { createEffect, createSignal } from "solid-js";
import type { JSX } from "solid-js";
import { useWorkflowsStore } from "../../stores/workflows-store.js";
import type { WorkflowTab } from "../../stores/workflows-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useUiStore } from "../../stores/ui-store.js";
import { useVisibleTabs } from "../../shared/hooks/use-visible-tabs.js";
import { SubTabBar } from "../../shared/components/sub-tab-bar.js";
import { subTabCycleBindings } from "../../shared/components/sub-tab-bar-utils.js";
import { useTabFallback } from "../../shared/hooks/use-tab-fallback.js";
import { WORKFLOW_TABS } from "../../shared/navigation.js";
import { BrickGate } from "../../shared/components/brick-gate.js";
import { ConfirmDialog } from "../../shared/components/confirm-dialog.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { WorkflowList } from "./workflow-list.js";
import { ExecutionList } from "./execution-list.js";
import { SchedulerView } from "./scheduler-view.js";
import { Tooltip } from "../../shared/components/tooltip.js";
import { textStyle } from "../../shared/text-style.js";

const HELP_TEXT: Readonly<Record<string, string>> = {
  workflows: "j/k:navigate  Tab:switch tab  e:execute  d:delete  p:enable/disable  r:refresh  Enter:detail  q:quit",
  executions: "j/k:navigate  Tab:switch tab  Enter:detail  Esc:close  r:refresh  q:quit",
  scheduler: "Tab:switch tab  r:refresh  q:quit",
};

export default function WorkflowsPanel(): JSX.Element {
  const client = useApi();

  const workflows = () => useWorkflowsStore((s) => s.workflows);
  const selectedWorkflowIndex = () => useWorkflowsStore((s) => s.selectedWorkflowIndex);
  const workflowsLoading = () => useWorkflowsStore((s) => s.workflowsLoading);
  const selectedWorkflow = () => useWorkflowsStore((s) => s.selectedWorkflow);
  const detailLoading = () => useWorkflowsStore((s) => s.detailLoading);
  const executions = () => useWorkflowsStore((s) => s.executions);
  const selectedExecutionIndex = () => useWorkflowsStore((s) => s.selectedExecutionIndex);
  const executionsLoading = () => useWorkflowsStore((s) => s.executionsLoading);
  const selectedExecution = () => useWorkflowsStore((s) => s.selectedExecution);
  const executionDetailLoading = () => useWorkflowsStore((s) => s.executionDetailLoading);
  const schedulerMetrics = () => useWorkflowsStore((s) => s.schedulerMetrics);
  const schedulerLoading = () => useWorkflowsStore((s) => s.schedulerLoading);
  const activeTab = () => useWorkflowsStore((s) => s.activeTab);
  const error = () => useWorkflowsStore((s) => s.error);

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

  const overlayActive = () => useUiStore((s) => s.overlayActive);
  const visibleTabs = useVisibleTabs(WORKFLOW_TABS);
  useTabFallback(visibleTabs, activeTab(), setActiveTab);

  // Track in-flight workflow execution
  const [executing, setExecuting] = createSignal(false);

  // Confirmation dialog state for destructive delete action
  const [confirmDelete, setConfirmDelete] = createSignal(false);

  const handleConfirmDelete = () => {
    if (!client) return;
    const s = useWorkflowsStore.getState();
    const wf = s.workflows[s.selectedWorkflowIndex];
    if (wf) {
      deleteWorkflow(wf.name, client);
    }
    setConfirmDelete(false);
  };

  const handleCancelDelete = () => {
    setConfirmDelete(false);
  };

  // Refresh current view based on active tab.
  // 'workflows' is intentionally excluded from deps — it is the fetch result,
  // not a trigger. Including it would create a loop (fetch updates workflows →
  // callback identity changes → effect re-fires → fetch again). We read from
  // the store via getState() at call time so the executions branch always
  // sees the latest value without making it a dependency.
  const refreshCurrentView = (): void => {
    if (!client) return;
    const tab = useWorkflowsStore.getState().activeTab;

    if (tab === "workflows") {
      fetchWorkflows(client);
    } else if (tab === "executions") {
      const { workflows: currentWorkflows, selectedWorkflowIndex: currentIdx } =
        useWorkflowsStore.getState();
      const wf = currentWorkflows[currentIdx];
      if (wf) fetchExecutions(wf.name, client);
    } else if (tab === "scheduler") {
      fetchSchedulerMetrics(client);
    }
  };

  // Auto-fetch when tab changes
  createEffect(() => {
    refreshCurrentView();
  });

  // Resolve the list length for current tab navigation (read fresh from store)
  const currentListLength = (): number => {
    const s = useWorkflowsStore.getState();
    if (s.activeTab === "workflows") return s.workflows.length;
    if (s.activeTab === "executions") return s.executions.length;
    return 0;
  };

  const currentIndex = (): number => {
    const s = useWorkflowsStore.getState();
    if (s.activeTab === "workflows") return s.selectedWorkflowIndex;
    if (s.activeTab === "executions") return s.selectedExecutionIndex;
    return 0;
  };

  const setCurrentIndex = (index: number): void => {
    const tab = useWorkflowsStore.getState().activeTab;
    if (tab === "workflows") {
      setSelectedWorkflowIndex(index);
    } else if (tab === "executions") {
      setSelectedExecutionIndex(index);
    }
  };

  useKeyboard(
    (): Record<string, () => void> => {
      if (useUiStore.getState().overlayActive) return {};
      if (confirmDelete()) return {}; // ConfirmDialog handles its own keys when visible

      const tab = useWorkflowsStore.getState().activeTab;
      return {
          ...listNavigationBindings({
            getIndex: currentIndex,
            setIndex: setCurrentIndex,
            getLength: currentListLength,
          }),
          ...subTabCycleBindings(visibleTabs, tab, setActiveTab),
          r: () => refreshCurrentView(),
          e: () => {
            const s = useWorkflowsStore.getState();
            if (s.activeTab !== "workflows" || !client) return;
            const wf = s.workflows[s.selectedWorkflowIndex];
            if (wf && wf.enabled) {
              setExecuting(true);
              executeWorkflow(wf.name, client).finally(() => setExecuting(false));
            }
          },
          d: () => {
            const s = useWorkflowsStore.getState();
            if (s.activeTab !== "workflows") return;
            const wf = s.workflows[s.selectedWorkflowIndex];
            if (wf) setConfirmDelete(true);
          },
          p: () => {
            const s = useWorkflowsStore.getState();
            if (s.activeTab !== "workflows" || !client) return;
            const wf = s.workflows[s.selectedWorkflowIndex];
            if (wf) {
              if (wf.enabled) disableWorkflow(wf.name, client);
              else enableWorkflow(wf.name, client);
            }
          },
          return: () => {
            if (!client) return;
            const s = useWorkflowsStore.getState();

            if (s.activeTab === "workflows") {
              const wf = s.workflows[s.selectedWorkflowIndex];
              if (wf) {
                fetchWorkflowDetail(wf.name, client);
              }
            } else if (s.activeTab === "executions") {
              const ex = s.executions[s.selectedExecutionIndex];
              if (ex) {
                // Toggle: if detail is shown for this execution, clear it
                if (s.selectedExecution?.execution_id === ex.execution_id) {
                  clearExecutionDetail();
                } else {
                  fetchExecutionDetail(ex.execution_id, client);
                }
              }
            }
          },
          escape: () => {
            // Clear expanded detail views
            const s = useWorkflowsStore.getState();
            if (s.activeTab === "executions" && s.selectedExecution) {
              clearExecutionDetail();
            }
          },
        };
    },
  );

  return (
    <BrickGate brick={["workflows", "scheduler"]}>
      <box height="100%" width="100%" flexDirection="column">
        <Tooltip tooltipKey="workflows-panel" message="Tip: Press ? for keybinding help" />
        {/* Tab bar */}
        <SubTabBar tabs={visibleTabs} activeTab={activeTab()} onSelect={setActiveTab as (id: string) => void} />

        {/* Error display */}
        {error() && (
          <box height={1} width="100%">
            <text>{`Error: ${error()}`}</text>
          </box>
        )}

        {/* Execution in-flight indicator */}
        {executing() && (
          <box height={1} width="100%">
            <LoadingIndicator message="Executing workflow..." centered={false} />
          </box>
        )}

        {/* Detail content */}
        <box flexGrow={1} borderStyle="single">
          {activeTab() === "workflows" && (
            <WorkflowList
              workflows={workflows()}
              selectedIndex={selectedWorkflowIndex()}
              loading={workflowsLoading()}
            />
          )}
          {activeTab() === "executions" && (
            <ExecutionList
              executions={executions()}
              selectedIndex={selectedExecutionIndex()}
              loading={executionsLoading()}
            />
          )}
          {activeTab() === "scheduler" && (
            <SchedulerView
              metrics={schedulerMetrics()}
              loading={schedulerLoading()}
            />
          )}
        </box>

        {/* Execution detail overlay when loaded */}
        {activeTab() === "executions" && executionDetailLoading() && (
          <box height={1} width="100%">
            <LoadingIndicator message="Loading execution detail..." centered={false} />
          </box>
        )}
        {activeTab() === "executions" && selectedExecution() && !executionDetailLoading() && (
          <box height={Math.min((selectedExecution()!.steps?.length ?? 0) + 3, 12)} width="100%" borderStyle="single" flexDirection="column">
            <text>
              {`Execution: ${selectedExecution()!.execution_id} | ${selectedExecution()!.status} | ${selectedExecution()!.actions_completed}/${selectedExecution()!.actions_total} actions`}
            </text>
            {(selectedExecution()!.steps ?? []).length > 0 ? (
              <scrollbox flexGrow={1} width="100%">
                {(selectedExecution()!.steps ?? []).map((step, i) => (
                  <box height={1} width="100%">
                    <text>
                      {`  ${String(step.step_index).padEnd(3)} ${(step.action_name ?? "").padEnd(20)} ${step.status.padEnd(10)} ${step.error_message ? `ERR: ${step.error_message}` : ""}`}
                    </text>
                  </box>
                ))}
              </scrollbox>
            ) : (
              <text style={textStyle({ dim: true })}>  No steps recorded</text>
            )}
          </box>
        )}

        {/* Workflow detail overlay when loaded */}
        {selectedWorkflow() && activeTab() === "workflows" && !detailLoading() && (
          <box height={3} width="100%">
            <text>
              {`Detail: ${selectedWorkflow()!.name} | v${selectedWorkflow()!.version} | ${selectedWorkflow()!.enabled ? "enabled" : "disabled"} | ${selectedWorkflow()!.triggers.length} triggers | ${selectedWorkflow()!.actions.length} actions`}
            </text>
          </box>
        )}

        {/* Help bar */}
        <box height={1} width="100%">
          <text>{HELP_TEXT[activeTab()] ?? ""}</text>
        </box>

        {/* Delete confirmation dialog */}
        <ConfirmDialog
          visible={confirmDelete()}
          title="Delete Workflow"
          message={`Permanently delete "${workflows()[selectedWorkflowIndex()]?.name ?? ""}"? This cannot be undone.`}
          onConfirm={handleConfirmDelete}
          onCancel={handleCancelDelete}
        />
      </box>
    </BrickGate>
  );
}
