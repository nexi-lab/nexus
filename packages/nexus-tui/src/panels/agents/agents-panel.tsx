/**
 * Agents panel: left sidebar with agent list, right pane with tabbed detail views.
 */

import React, { useEffect } from "react";
import { useAgentsStore } from "../../stores/agents-store.js";
import type { AgentTab } from "../../stores/agents-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useVisibleTabs, type TabDef } from "../../shared/hooks/use-visible-tabs.js";
import { AgentStatusView } from "./agent-status-view.js";
import { DelegationList } from "./delegation-list.js";
import { InboxView } from "./inbox-view.js";
import { TrajectoriesTab } from "./trajectories-tab.js";

const ALL_TABS: readonly TabDef<AgentTab>[] = [
  { id: "status", label: "Status", brick: "agent_registry" },
  { id: "delegations", label: "Delegations", brick: "delegation" },
  { id: "inbox", label: "Inbox", brick: "ipc" },
  { id: "trajectories", label: "Trajectories", brick: "agent_registry" },
];
const TAB_LABELS: Readonly<Record<AgentTab, string>> = {
  status: "Status",
  delegations: "Delegations",
  inbox: "Inbox",
  trajectories: "Trajectories",
};

export default function AgentsPanel(): React.ReactNode {
  const client = useApi();
  const visibleTabs = useVisibleTabs(ALL_TABS);

  // Zone ID for fetchAgents
  const configZoneId = useGlobalStore((s) => s.config.zoneId);
  const serverZoneId = useGlobalStore((s) => s.zoneId);
  const effectiveZoneId = configZoneId ?? serverZoneId ?? undefined;

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
  const fetchDelegations = useAgentsStore((s) => s.fetchDelegations);
  const fetchInbox = useAgentsStore((s) => s.fetchInbox);
  const fetchTrajectories = useAgentsStore((s) => s.fetchTrajectories);
  const revokeDelegation = useAgentsStore((s) => s.revokeDelegation);
  const warmupAgent = useAgentsStore((s) => s.warmupAgent);
  const evictAgent = useAgentsStore((s) => s.evictAgent);
  const verifyAgent = useAgentsStore((s) => s.verifyAgent);
  const setSelectedDelegationIndex = useAgentsStore((s) => s.setSelectedDelegationIndex);

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
      fetchAgentStatus(selectedAgentId, client);
      fetchAgentSpec(selectedAgentId, client);
      fetchAgentIdentity(selectedAgentId, client);
    } else if (activeTab === "delegations") {
      fetchDelegations(client);
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

  useKeyboard({
    j: () => {
      if (activeTab === "delegations") {
        setSelectedDelegationIndex(
          Math.min(selectedDelegationIndex + 1, delegations.length - 1),
        );
      } else {
        setSelectedAgentIndex(Math.min(selectedAgentIndex + 1, displayAgentIds.length - 1));
        const nextAgent = displayAgentIds[selectedAgentIndex + 1];
        if (nextAgent) {
          setSelectedAgentId(nextAgent);
        }
      }
    },
    down: () => {
      if (activeTab === "delegations") {
        setSelectedDelegationIndex(
          Math.min(selectedDelegationIndex + 1, delegations.length - 1),
        );
      } else {
        setSelectedAgentIndex(Math.min(selectedAgentIndex + 1, displayAgentIds.length - 1));
        const nextAgent = displayAgentIds[selectedAgentIndex + 1];
        if (nextAgent) {
          setSelectedAgentId(nextAgent);
        }
      }
    },
    k: () => {
      if (activeTab === "delegations") {
        setSelectedDelegationIndex(Math.max(selectedDelegationIndex - 1, 0));
      } else {
        setSelectedAgentIndex(Math.max(selectedAgentIndex - 1, 0));
        const prevAgent = displayAgentIds[selectedAgentIndex - 1];
        if (prevAgent) {
          setSelectedAgentId(prevAgent);
        }
      }
    },
    up: () => {
      if (activeTab === "delegations") {
        setSelectedDelegationIndex(Math.max(selectedDelegationIndex - 1, 0));
      } else {
        setSelectedAgentIndex(Math.max(selectedAgentIndex - 1, 0));
        const prevAgent = displayAgentIds[selectedAgentIndex - 1];
        if (prevAgent) {
          setSelectedAgentId(prevAgent);
        }
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
    r: () => refreshCurrentView(),
    d: () => {
      if (activeTab !== "delegations" || !client) return;
      const selected = delegations[selectedDelegationIndex];
      if (selected && selected.status === "active") {
        revokeDelegation(selected.delegation_id, client);
      }
    },
    return: () => {
      // If an agent is highlighted in the agents list, select it
      const agent = displayAgentIds[selectedAgentIndex];
      if (agent) {
        setSelectedAgentId(agent);
        addKnownAgent(agent);
      }
    },
    "shift+w": () => {
      if (!client || !selectedAgentId) return;
      warmupAgent(selectedAgentId, client);
    },
    "shift+e": () => {
      if (!client || !selectedAgentId) return;
      // Only evict if agent is not already evicted
      const agentEntry = agents.find((a) => a.agent_id === selectedAgentId);
      if (agentEntry && agentEntry.state === "evicted") return;
      evictAgent(selectedAgentId, client);
    },
    "shift+v": () => {
      if (!client || !selectedAgentId) return;
      verifyAgent(selectedAgentId, client);
    },
  });

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Main content */}
      <box flexGrow={1} flexDirection="row">
        {/* Left sidebar: agent list (30%) */}
        <box width="30%" height="100%" borderStyle="single" flexDirection="column">
          <box height={1} width="100%">
            <text>{agentsLoading ? "--- Agents (loading...) ---" : "--- Agents ---"}</text>
          </box>

          {/* Agents list */}
          {displayAgentIds.length === 0 ? (
            <box flexGrow={1} justifyContent="center" alignItems="center">
              <text>No agents tracked</text>
            </box>
          ) : (
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
          )}
        </box>

        {/* Right pane: detail views (70%) */}
        <box width="70%" height="100%" flexDirection="column">
          {/* Tab bar */}
          <box height={1} width="100%">
            <text>
              {visibleTabs.map((tab) => {
                return tab.id === activeTab ? `[${tab.label}]` : ` ${tab.label} `;
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
            {activeTab === "status" && (
              <AgentStatusView
                status={agentStatus}
                spec={agentSpec}
                identity={agentIdentity}
                loading={statusLoading}
              />
            )}
            {activeTab === "delegations" && (
              <DelegationList
                delegations={delegations}
                selectedIndex={selectedDelegationIndex}
                loading={delegationsLoading}
              />
            )}
            {activeTab === "inbox" && (
              <InboxView
                messages={inboxMessages}
                count={inboxCount}
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

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {"j/k:navigate  Tab:switch tab  r:refresh  d:revoke  Shift+W:warmup  Shift+E:evict  Shift+V:verify  Enter:select  q:quit"}
        </text>
      </box>
    </box>
  );
}
