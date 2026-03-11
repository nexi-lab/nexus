/**
 * Agents panel: left sidebar with agent list, right pane with tabbed detail views.
 */

import React, { useEffect } from "react";
import { useAgentsStore } from "../../stores/agents-store.js";
import type { AgentTab } from "../../stores/agents-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { AgentStatusView } from "./agent-status-view.js";
import { DelegationList } from "./delegation-list.js";
import { InboxView } from "./inbox-view.js";

const TAB_ORDER: readonly AgentTab[] = ["status", "delegations", "inbox"];
const TAB_LABELS: Readonly<Record<AgentTab, string>> = {
  status: "Status",
  delegations: "Delegations",
  inbox: "Inbox",
};

export default function AgentsPanel(): React.ReactNode {
  const client = useApi();

  const knownAgents = useAgentsStore((s) => s.knownAgents);
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
  const error = useAgentsStore((s) => s.error);

  const setSelectedAgentId = useAgentsStore((s) => s.setSelectedAgentId);
  const setSelectedAgentIndex = useAgentsStore((s) => s.setSelectedAgentIndex);
  const setActiveTab = useAgentsStore((s) => s.setActiveTab);
  const addKnownAgent = useAgentsStore((s) => s.addKnownAgent);
  const fetchAgentStatus = useAgentsStore((s) => s.fetchAgentStatus);
  const fetchAgentSpec = useAgentsStore((s) => s.fetchAgentSpec);
  const fetchAgentIdentity = useAgentsStore((s) => s.fetchAgentIdentity);
  const fetchDelegations = useAgentsStore((s) => s.fetchDelegations);
  const fetchInbox = useAgentsStore((s) => s.fetchInbox);
  const revokeDelegation = useAgentsStore((s) => s.revokeDelegation);
  const setSelectedDelegationIndex = useAgentsStore((s) => s.setSelectedDelegationIndex);

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
        setSelectedAgentIndex(Math.min(selectedAgentIndex + 1, knownAgents.length - 1));
        const nextAgent = knownAgents[selectedAgentIndex + 1];
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
        setSelectedAgentIndex(Math.min(selectedAgentIndex + 1, knownAgents.length - 1));
        const nextAgent = knownAgents[selectedAgentIndex + 1];
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
        const prevAgent = knownAgents[selectedAgentIndex - 1];
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
        const prevAgent = knownAgents[selectedAgentIndex - 1];
        if (prevAgent) {
          setSelectedAgentId(prevAgent);
        }
      }
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
    d: () => {
      if (activeTab !== "delegations" || !client) return;
      const selected = delegations[selectedDelegationIndex];
      if (selected && selected.status === "active") {
        revokeDelegation(selected.delegation_id, client);
      }
    },
    return: () => {
      // If an agent is highlighted in the known agents list, select it
      const agent = knownAgents[selectedAgentIndex];
      if (agent) {
        setSelectedAgentId(agent);
        addKnownAgent(agent);
      }
    },
  });

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Main content */}
      <box flexGrow={1} flexDirection="row">
        {/* Left sidebar: agent list (30%) */}
        <box width="30%" height="100%" borderStyle="single" flexDirection="column">
          <box height={1} width="100%">
            <text>{"--- Agents ---"}</text>
          </box>

          {/* Known agents list */}
          {knownAgents.length === 0 ? (
            <box flexGrow={1} justifyContent="center" alignItems="center">
              <text>No agents tracked</text>
            </box>
          ) : (
            <scrollbox flexGrow={1} width="100%">
              {knownAgents.map((agentId, i) => {
                const isSelected = i === selectedAgentIndex;
                const isActive = agentId === selectedAgentId;
                const prefix = isSelected ? "> " : "  ";
                const suffix = isActive ? " *" : "";
                return (
                  <box key={agentId} height={1} width="100%">
                    <text>{`${prefix}${agentId}${suffix}`}</text>
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
          </box>
        </box>
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {"j/k:navigate  Tab:switch tab  r:refresh  d:revoke delegation  Enter:select  q:quit"}
        </text>
      </box>
    </box>
  );
}
