/**
 * Agents panel: left sidebar with agent list, right pane with tabbed detail views.
 */

import React, { useEffect, useState } from "react";
import { useAgentsStore } from "../../stores/agents-store.js";
import type { AgentTab, DelegationItem } from "../../stores/agents-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import { jumpToStart, jumpToEnd } from "../../shared/hooks/use-list-navigation.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useVisibleTabs, type TabDef } from "../../shared/hooks/use-visible-tabs.js";
import { AgentStatusView } from "./agent-status-view.js";
import { DelegationList } from "./delegation-list.js";
import { InboxView } from "./inbox-view.js";
import { TrajectoriesTab } from "./trajectories-tab.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { StyledText } from "../../shared/components/styled-text.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { CommandOutput } from "../../shared/components/command-output.js";
import { useCommandRunnerStore, executeLocalCommand } from "../../services/command-runner.js";
import { useUiStore } from "../../stores/ui-store.js";
import { focusColor } from "../../shared/theme.js";
import { ScrollIndicator } from "../../shared/components/scroll-indicator.js";

const ALL_TABS: readonly TabDef<AgentTab>[] = [
  { id: "status", label: "Status", brick: "agent_runtime" },
  { id: "delegations", label: "Delegations", brick: "delegation" },
  { id: "inbox", label: "Inbox", brick: "ipc" },
  { id: "trajectories", label: "Trajectories", brick: "agent_runtime" },
];
const TAB_LABELS: Readonly<Record<AgentTab, string>> = {
  status: "Status",
  delegations: "Delegations",
  inbox: "Inbox",
  trajectories: "Trajectories",
};

export default function AgentsPanel(): React.ReactNode {
  const client = useApi();
  const confirm = useConfirmStore((s) => s.confirm);
  const visibleTabs = useVisibleTabs(ALL_TABS);

  // Reactive subscription to command runner status (Codex finding 2)
  const commandRunnerStatus = useCommandRunnerStore((s) => s.status);

  // Zone ID for fetchAgents
  const configZoneId = useGlobalStore((s) => s.config.zoneId);
  const serverZoneId = useGlobalStore((s) => s.zoneId);
  const effectiveZoneId = configZoneId ?? serverZoneId ?? "root";

  const knownAgents = useAgentsStore((s) => s.knownAgents);
  const agents = useAgentsStore((s) => s.agents);
  const agentsLoading = useAgentsStore((s) => s.agentsLoading);
  const selectedAgentId = useAgentsStore((s) => s.selectedAgentId);
  const selectedAgentIndex = useAgentsStore((s) => s.selectedAgentIndex);
  const activeTab = useAgentsStore((s) => s.activeTab);
  const agentStatus = useAgentsStore((s) => s.agentStatus);
  const agentSpec = useAgentsStore((s) => s.agentSpec);
  const agentIdentity = useAgentsStore((s) => s.agentIdentity);
  const statusLoading = useAgentsStore((s) => s.statusLoading);
  const trustScore = useAgentsStore((s) => s.trustScore);
  const reputation = useAgentsStore((s) => s.reputation);
  const delegations = useAgentsStore((s) => s.delegations);
  const delegationsLoading = useAgentsStore((s) => s.delegationsLoading);
  const selectedDelegationIndex = useAgentsStore((s) => s.selectedDelegationIndex);
  const inboxMessages = useAgentsStore((s) => s.inboxMessages);
  const inboxCount = useAgentsStore((s) => s.inboxCount);
  const inboxLoading = useAgentsStore((s) => s.inboxLoading);
  const trajectories = useAgentsStore((s) => s.trajectories);
  const trajectoriesLoading = useAgentsStore((s) => s.trajectoriesLoading);
  const error = useAgentsStore((s) => s.error);

  const setSelectedAgentId = useAgentsStore((s) => s.setSelectedAgentId);
  const setSelectedAgentIndex = useAgentsStore((s) => s.setSelectedAgentIndex);
  const setActiveTab = useAgentsStore((s) => s.setActiveTab);
  const addKnownAgent = useAgentsStore((s) => s.addKnownAgent);
  const fetchAgents = useAgentsStore((s) => s.fetchAgents);
  const fetchAgentStatus = useAgentsStore((s) => s.fetchAgentStatus);
  const fetchAgentSpec = useAgentsStore((s) => s.fetchAgentSpec);
  const fetchAgentIdentity = useAgentsStore((s) => s.fetchAgentIdentity);
  const fetchTrustScore = useAgentsStore((s) => s.fetchTrustScore);
  const fetchAgentReputation = useAgentsStore((s) => s.fetchAgentReputation);
  const fetchDelegations = useAgentsStore((s) => s.fetchDelegations);
  const fetchInbox = useAgentsStore((s) => s.fetchInbox);
  const fetchTrajectories = useAgentsStore((s) => s.fetchTrajectories);
  const revokeDelegation = useAgentsStore((s) => s.revokeDelegation);
  const warmupAgent = useAgentsStore((s) => s.warmupAgent);
  const evictAgent = useAgentsStore((s) => s.evictAgent);
  const verifyAgent = useAgentsStore((s) => s.verifyAgent);
  const setSelectedDelegationIndex = useAgentsStore((s) => s.setSelectedDelegationIndex);

  // Focus pane (ui-store)
  const uiFocusPane = useUiStore((s) => s.getFocusPane("agents"));
  const toggleFocus = useUiStore((s) => s.toggleFocusPane);
  const overlayActive = useUiStore((s) => s.overlayActive);

  // Clipboard copy
  const { copy, copied } = useCopy();

  // Local loading state for async warmup/evict/verify operations
  const [operationLoading, setOperationLoading] = useState<string | null>(null);

  // Expanded delegation detail
  const [expandedDelegation, setExpandedDelegation] = useState<DelegationItem | null>(null);

  // Merge fetched agents into a display list: fetched agents + any manually added knownAgents not in the fetched list
  const fetchedAgentIds = agents.map((a) => a.agent_id);
  const extraKnown = knownAgents.filter((id) => !fetchedAgentIds.includes(id));
  const displayAgentIds = [...fetchedAgentIds, ...extraKnown];

  // Fetch agents on mount when zone is available
  useEffect(() => {
    if (client && effectiveZoneId) {
      fetchAgents(effectiveZoneId, client);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, effectiveZoneId]);

  // Fall back to first visible tab if the active tab becomes hidden
  const visibleIds = visibleTabs.map((t) => t.id);
  useEffect(() => {
    if (visibleIds.length > 0 && !visibleIds.includes(activeTab)) {
      setActiveTab(visibleIds[0]!);
    }
  }, [visibleIds.join(","), activeTab, setActiveTab]);

  // Refresh current view based on active tab
  const refreshCurrentView = (): void => {
    if (!client) return;

    if (activeTab === "status" && selectedAgentId) {
      // Fetch permissions for all agents (works for registered + running)
      client.get<{ permissions: readonly { relation: string; object_type: string; object_id: string }[] }>(
        `/api/v2/agents/${encodeURIComponent(selectedAgentId)}/permissions`,
      ).then((r) => useAgentsStore.setState({ agentPermissions: r.permissions }))
        .catch(() => useAgentsStore.setState({ agentPermissions: [] }));
      // Only fetch live status for running agents
      const selectedAgent = agents.find((a) => a.agent_id === selectedAgentId);
      if (selectedAgent && selectedAgent.state !== "registered") {
        fetchAgentStatus(selectedAgentId, client);
        fetchAgentSpec(selectedAgentId, client);
        fetchAgentIdentity(selectedAgentId, client);
        fetchTrustScore(selectedAgentId, client);
        fetchAgentReputation(selectedAgentId, client);
      }
    } else if (activeTab === "delegations" && selectedAgentId) {
      fetchDelegations(selectedAgentId, client);
    } else if (activeTab === "inbox" && selectedAgentId) {
      fetchInbox(selectedAgentId, client);
    } else if (activeTab === "trajectories" && selectedAgentId) {
      fetchTrajectories(selectedAgentId, client);
    }

    // Also refresh agent list
    if (effectiveZoneId) {
      fetchAgents(effectiveZoneId, client);
    }
  };

  // Auto-fetch when agent or tab changes
  useEffect(() => {
    refreshCurrentView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAgentId, activeTab, client]);

  useKeyboard(overlayActive ? {} : {
    j: () => {
      if (activeTab === "delegations") {
        if (delegations.length === 0) return;
        setSelectedDelegationIndex(
          Math.max(0, Math.min(selectedDelegationIndex + 1, delegations.length - 1)),
        );
      } else {
        if (displayAgentIds.length === 0) return;
        const newIdx = Math.max(0, Math.min(selectedAgentIndex + 1, displayAgentIds.length - 1));
        setSelectedAgentIndex(newIdx);
        const agentId = displayAgentIds[newIdx];
        if (agentId) setSelectedAgentId(agentId);
      }
    },
    down: () => {
      if (activeTab === "delegations") {
        if (delegations.length === 0) return;
        setSelectedDelegationIndex(
          Math.max(0, Math.min(selectedDelegationIndex + 1, delegations.length - 1)),
        );
      } else {
        if (displayAgentIds.length === 0) return;
        const newIdx = Math.max(0, Math.min(selectedAgentIndex + 1, displayAgentIds.length - 1));
        setSelectedAgentIndex(newIdx);
        const agentId = displayAgentIds[newIdx];
        if (agentId) setSelectedAgentId(agentId);
      }
    },
    k: () => {
      if (activeTab === "delegations") {
        setSelectedDelegationIndex(Math.max(selectedDelegationIndex - 1, 0));
      } else {
        const newIdx = Math.max(0, selectedAgentIndex - 1);
        setSelectedAgentIndex(newIdx);
        const agentId = displayAgentIds[newIdx];
        if (agentId) setSelectedAgentId(agentId);
      }
    },
    up: () => {
      if (activeTab === "delegations") {
        setSelectedDelegationIndex(Math.max(selectedDelegationIndex - 1, 0));
      } else {
        const newIdx = Math.max(0, selectedAgentIndex - 1);
        setSelectedAgentIndex(newIdx);
        const agentId = displayAgentIds[newIdx];
        if (agentId) setSelectedAgentId(agentId);
      }
    },
    tab: () => {
      const ids = visibleTabs.map((t) => t.id);
      const currentIdx = ids.indexOf(activeTab);
      const nextIdx = (currentIdx + 1) % ids.length;
      const nextTab = ids[nextIdx];
      if (nextTab) {
        setActiveTab(nextTab);
      }
    },
    "shift+tab": () => toggleFocus("agents"),
    r: () => refreshCurrentView(),
    d: async () => {
      if (activeTab !== "delegations" || !client) return;
      const selected = delegations[selectedDelegationIndex];
      if (selected && selected.status === "active") {
        const ok = await confirm("Revoke delegation?", `Revoke delegation ${selected.delegation_id}. The agent will lose delegated access.`);
        if (!ok) return;
        revokeDelegation(selected.delegation_id, client);
      }
    },
    return: () => {
      if (activeTab === "delegations") {
        // Toggle delegation detail drill-down
        const selected = delegations[selectedDelegationIndex];
        if (selected) {
          setExpandedDelegation(
            expandedDelegation?.delegation_id === selected.delegation_id ? null : selected,
          );
        }
        return;
      }
      // If an agent is highlighted in the agents list, select it
      const agent = displayAgentIds[selectedAgentIndex];
      if (agent) {
        setSelectedAgentId(agent);
        addKnownAgent(agent);
      }
    },
    escape: () => {
      if (expandedDelegation) {
        setExpandedDelegation(null);
      }
    },
    "shift+w": async () => {
      if (!client || !selectedAgentId) return;
      setOperationLoading("Warming up agent...");
      try {
        await warmupAgent(selectedAgentId, client);
      } finally {
        setOperationLoading(null);
      }
    },
    "shift+e": async () => {
      if (!client || !selectedAgentId) return;
      // Only evict if agent is not already evicted
      const agentEntry = agents.find((a) => a.agent_id === selectedAgentId);
      if (agentEntry && agentEntry.state === "evicted") return;
      setOperationLoading("Evicting agent...");
      try {
        await evictAgent(selectedAgentId, client);
      } finally {
        setOperationLoading(null);
      }
    },
    "shift+v": async () => {
      if (!client || !selectedAgentId) return;
      setOperationLoading("Verifying agent...");
      try {
        await verifyAgent(selectedAgentId, client);
      } finally {
        setOperationLoading(null);
      }
    },
    g: () => {
      if (activeTab === "delegations") {
        setSelectedDelegationIndex(jumpToStart());
      } else {
        setSelectedAgentIndex(jumpToStart());
        const firstAgent = displayAgentIds[0];
        if (firstAgent) {
          setSelectedAgentId(firstAgent);
        }
      }
    },
    "shift+g": () => {
      if (activeTab === "delegations") {
        setSelectedDelegationIndex(jumpToEnd(delegations.length));
      } else {
        const lastIdx = jumpToEnd(displayAgentIds.length);
        setSelectedAgentIndex(lastIdx);
        const lastAgent = displayAgentIds[lastIdx];
        if (lastAgent) {
          setSelectedAgentId(lastAgent);
        }
      }
    },
    y: () => {
      if (selectedAgentId) {
        copy(selectedAgentId);
      }
    },
    // Issue #3078: spawn new agent via local CLI command
    n: () => {
      useCommandRunnerStore.getState().reset();
      executeLocalCommand("agent", ["spawn"]);
    },
  });

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Main content */}
      <box flexGrow={1} flexDirection="row">
        {/* Left sidebar: agent list (30%) */}
        <box width="30%" height="100%" borderStyle="single" borderColor={uiFocusPane === "left" ? focusColor.activeBorder : focusColor.inactiveBorder} flexDirection="column">
          <box height={1} width="100%">
            {agentsLoading
              ? <LoadingIndicator message="Agents" centered={false} />
              : <text>{"--- Agents ---"}</text>}
          </box>

          {/* Agents list */}
          {displayAgentIds.length === 0 ? (
            <EmptyState
              message="No agents registered."
              hint="Start an agent with 'nexus agent spawn' or add one with the API."
            />
          ) : (
            <ScrollIndicator selectedIndex={selectedAgentIndex} totalItems={displayAgentIds.length} visibleItems={20}>
              <scrollbox flexGrow={1} width="100%">
                {displayAgentIds.map((agentId, i) => {
                  const isSelected = i === selectedAgentIndex;
                  const isActive = agentId === selectedAgentId;
                  const prefix = isSelected ? "> " : "  ";
                  const suffix = isActive ? " *" : "";
                  const agentEntry = agents.find((a) => a.agent_id === agentId);
                  const stateLabel = agentEntry ? ` [${agentEntry.state}]` : "";
                  return (
                    <box key={agentId} height={1} width="100%">
                      <text>{`${prefix}${agentId}${stateLabel}${suffix}`}</text>
                    </box>
                  );
                })}
              </scrollbox>
            </ScrollIndicator>
          )}
        </box>

        {/* Right pane: detail views (70%) */}
        <box width="70%" height="100%" borderStyle="single" borderColor={uiFocusPane === "right" ? focusColor.activeBorder : focusColor.inactiveBorder} flexDirection="column">
          {/* Tab bar */}
          <box height={1} width="100%">
            <text>
              {visibleTabs.map((tab) => {
                return tab.id === activeTab ? `[${tab.label}]` : ` ${tab.label} `;
              }).join(" ")}
            </text>
          </box>

          {/* Operation in-progress feedback */}
          {operationLoading && (
            <box height={1} width="100%">
              <LoadingIndicator message={operationLoading} centered={false} />
            </box>
          )}

          {/* Error display */}
          {error && (
            <box height={1} width="100%">
              <StyledText>{`Error: ${error}`}</StyledText>
            </box>
          )}

          {/* Detail content */}
          <box flexGrow={1} borderStyle="single">
            {activeTab === "status" && (() => {
              const selectedAgent = agents.find((a) => a.agent_id === selectedAgentId);
              if (selectedAgent?.state === "registered") {
                const perms = useAgentsStore.getState().agentPermissions;
                return (
                  <box height="100%" width="100%" flexDirection="column" padding={1}>
                    <text bold>{`Agent: ${selectedAgent.agent_id}`}</text>
                    <text>{""}</text>
                    <text><span foregroundColor="cyan">{"State:  "}</span><span>{"registered"}</span></text>
                    <text><span foregroundColor="cyan">{"Name:   "}</span><span>{selectedAgent.name ?? selectedAgent.agent_id}</span></text>
                    <text><span foregroundColor="cyan">{"Owner:  "}</span><span>{selectedAgent.owner_id}</span></text>
                    <text><span foregroundColor="cyan">{"Zone:   "}</span><span>{selectedAgent.zone_id ?? "root"}</span></text>
                    <text>{""}</text>
                    <text bold foregroundColor="cyan">{"Permissions:"}</text>
                    {perms.length === 0 ? (
                      <text dimColor>{"  No permissions assigned"}</text>
                    ) : (
                      perms.map((p, i) => (
                        <text key={`perm-${i}`}>
                          <span foregroundColor="green">{`  ${p.relation}`}</span>
                          <span dimColor>{" on "}</span>
                          <span foregroundColor="blue">{`${p.object_type}:${p.object_id}`}</span>
                        </text>
                      ))
                    )}
                    <text>{""}</text>
                    <text dimColor>{"Agent is registered but not running."}</text>
                  </box>
                );
              }
              return (
                <AgentStatusView
                  status={agentStatus}
                  spec={agentSpec}
                  identity={agentIdentity}
                  loading={statusLoading}
                  trustScore={trustScore}
                  reputation={reputation}
                />
              );
            })()}
            {activeTab === "delegations" && (
              <DelegationList
                delegations={delegations}
                selectedIndex={selectedDelegationIndex}
                loading={delegationsLoading}
                expandedDelegation={expandedDelegation}
              />
            )}
            {activeTab === "inbox" && (
              <InboxView
                messages={inboxMessages}
                count={inboxCount}
                processedMessages={useAgentsStore.getState().processedMessages}
                deadLetterMessages={useAgentsStore.getState().deadLetterMessages}
                loading={inboxLoading}
              />
            )}
            {activeTab === "trajectories" && (
              <TrajectoriesTab
                trajectories={trajectories}
                loading={trajectoriesLoading}
              />
            )}
          </box>
        </box>
      </box>

      {/* Command runner output (when agent spawn is running) */}
      {commandRunnerStatus !== "idle" && (
        <box borderStyle="single" height={6} width="100%">
          <CommandOutput />
        </box>
      )}

      {/* Help bar */}
      <box height={1} width="100%">
        {copied
          ? <text foregroundColor="green">Copied!</text>
          : <text>
          {"j/k:navigate  Tab:switch tab  r:refresh  n:spawn agent  Enter:detail  d:revoke  Shift+W:warmup  Shift+E:evict  y:copy  q:quit"}
        </text>}
      </box>
    </box>
  );
}
