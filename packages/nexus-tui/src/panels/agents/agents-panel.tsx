/**
 * Agents panel: left sidebar with agent list, right pane with tabbed detail views.
 */

import { createEffect, createSignal, Match, Switch } from "solid-js";
import type { JSX } from "solid-js";
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
import { SubTabBar } from "../../shared/components/sub-tab-bar.js";
import { CommandOutput } from "../../shared/components/command-output.js";
import { useCommandRunnerStore, executeLocalCommand } from "../../services/command-runner.js";
import { useUiStore } from "../../stores/ui-store.js";
import { agentStateColor, focusColor, statusColor } from "../../shared/theme.js";
import { ScrollIndicator } from "../../shared/components/scroll-indicator.js";
import { textStyle } from "../../shared/text-style.js";

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

export default function AgentsPanel(): JSX.Element {
  const client = useApi();
  const confirm = useConfirmStore((s) => s.confirm);
  const visibleTabs = useVisibleTabs(ALL_TABS);

  // Reactive subscription to command runner status (Codex finding 2)
  const commandRunnerStatus = useCommandRunnerStore((s) => s.status);

  // Zone ID for fetchAgents
  const configZoneId = useGlobalStore((s) => s.config.zoneId);
  const serverZoneId = useGlobalStore((s) => s.zoneId);
  const effectiveZoneId = configZoneId ?? serverZoneId ?? "root";

  // Read store state reactively through the Solid proxy (jsx:"preserve" compiles
  // these into reactive getters when used in JSX expressions).
  const as = useAgentsStore();  // Solid store proxy — reads are tracked in JSX
  // Shorthand accessors used in JSX and effects:
  const agents = () => as.agents;
  const agentsLoading = () => as.agentsLoading;
  const selectedAgentId = () => as.selectedAgentId;
  const selectedAgentIndex = () => as.selectedAgentIndex;
  const activeTab = () => as.activeTab;
  const agentStatus = () => as.agentStatus;
  const agentSpec = () => as.agentSpec;
  const agentIdentity = () => as.agentIdentity;
  const statusLoading = () => as.statusLoading;
  const trustScore = () => as.trustScore;
  const reputation = () => as.reputation;
  const delegations = () => as.delegations;
  const delegationsLoading = () => as.delegationsLoading;
  const selectedDelegationIndex = () => as.selectedDelegationIndex;
  const inboxMessages = () => as.inboxMessages;
  const inboxCount = () => as.inboxCount;
  const inboxLoading = () => as.inboxLoading;
  const trajectories = () => as.trajectories;
  const trajectoriesLoading = () => as.trajectoriesLoading;
  const error = () => as.error;
  const knownAgents = () => as.knownAgents;

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
  const [operationLoading, setOperationLoading] = createSignal<string | null>(null);

  // Expanded delegation detail
  const [expandedDelegation, setExpandedDelegation] = createSignal<DelegationItem | null>(null);

  // Merge fetched agents into a display list — call as displayAgentIds() for fresh data
  const displayAgentIds = () => {
    const a = agents();
    const k = knownAgents();
    const fetchedIds = a.map((ag) => ag.agent_id);
    const extra = k.filter((id) => !fetchedIds.includes(id));
    return [...fetchedIds, ...extra];
  };

  // Fetch agents on mount when zone is available
  createEffect(() => {
    if (client && effectiveZoneId) {
      fetchAgents(effectiveZoneId, client);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  });

  // Auto-select first agent when list loads
  createEffect(() => {
    const ids = displayAgentIds();
    if (ids.length > 0 && !selectedAgentId()) {
      setSelectedAgentId(ids[0]!);
      setSelectedAgentIndex(0);
    }
  });

  // Fall back to first visible tab if the active tab becomes hidden
  const visibleIds = visibleTabs.map((t) => t.id);
  createEffect(() => {
    if (visibleIds.length > 0 && !visibleIds.includes(activeTab())) {
      setActiveTab(visibleIds[0]!);
    }
  });

  // Refresh current view based on active tab
  const refreshCurrentView = (): void => {
    if (!client) return;
    const tab = activeTab();
    const aid = selectedAgentId();

    if (tab === "status" && aid) {
      client.get<{ permissions: readonly { relation: string; object_type: string; object_id: string }[] }>(
        `/api/v2/agents/${encodeURIComponent(aid)}/permissions`,
      ).then((r) => useAgentsStore.setState({ agentPermissions: r.permissions }))
        .catch(() => useAgentsStore.setState({ agentPermissions: [] }));
      fetchAgentStatus(aid, client);
      fetchAgentSpec(aid, client);
      fetchAgentIdentity(aid, client);
      fetchTrustScore(aid, client);
      fetchAgentReputation(aid, client);
    } else if (tab === "delegations" && aid) {
      fetchDelegations(aid, client);
    } else if (tab === "inbox" && aid) {
      fetchInbox(aid, client);
    } else if (tab === "trajectories" && aid) {
      fetchTrajectories(aid, client);
    }

    // Also refresh agent list
    if (effectiveZoneId) {
      fetchAgents(effectiveZoneId, client);
    }
  };

  // Auto-fetch when agent or tab changes
  createEffect(() => {
    // Track reactive deps so this re-runs when tab or agent changes
    void activeTab();
    void selectedAgentId();
    refreshCurrentView();
  });

  useKeyboard((): Record<string, () => void> => {
    if (useUiStore.getState().overlayActive) return {};
    const s = useAgentsStore.getState();
    const move = (delta: number) => () => {
      if (s.activeTab === "delegations") {
        const next = Math.max(0, Math.min(s.selectedDelegationIndex + delta, s.delegations.length - 1));
        setSelectedDelegationIndex(next);
      } else {
        const ids = displayAgentIds();
        if (ids.length === 0) return;
        const next = Math.max(0, Math.min(s.selectedAgentIndex + delta, ids.length - 1));
        setSelectedAgentIndex(next);
        const agentId = ids[next];
        if (agentId) setSelectedAgentId(agentId);
      }
    };
    return {
      j: move(1), down: move(1), k: move(-1), up: move(-1),
      tab: () => {
        const ids = visibleTabs.map((t) => t.id);
        const currentIdx = ids.indexOf(s.activeTab);
        const nextIdx = (currentIdx + 1) % ids.length;
        const nextTab = ids[nextIdx];
        if (nextTab) setActiveTab(nextTab);
      },
      "shift+tab": () => toggleFocus("agents"),
      r: () => refreshCurrentView(),
      d: async () => {
        const st = useAgentsStore.getState();
        if (st.activeTab !== "delegations" || !client) return;
        const selected = st.delegations[st.selectedDelegationIndex];
        if (selected && selected.status === "active") {
          const ok = await confirm("Revoke delegation?", `Revoke delegation ${selected.delegation_id}. The agent will lose delegated access.`);
          if (!ok) return;
          revokeDelegation(selected.delegation_id, client);
        }
      },
      return: () => {
        const st = useAgentsStore.getState();
        if (st.activeTab === "delegations") {
          const selected = st.delegations[st.selectedDelegationIndex];
          if (selected) {
            setExpandedDelegation(
              expandedDelegation()?.delegation_id === selected.delegation_id ? null : selected,
            );
          }
          return;
        }
        const agent = displayAgentIds()[st.selectedAgentIndex];
        if (agent) { setSelectedAgentId(agent); addKnownAgent(agent); }
      },
      escape: () => { if (expandedDelegation()) setExpandedDelegation(null); },
      "shift+w": async () => {
        const aid = useAgentsStore.getState().selectedAgentId;
        if (!client || !aid) return;
        setOperationLoading("Warming up agent...");
        try { await warmupAgent(aid, client); } finally { setOperationLoading(null); }
      },
      "shift+e": async () => {
        const st = useAgentsStore.getState();
        if (!client || !st.selectedAgentId) return;
        const entry = st.agents.find((a) => a.agent_id === st.selectedAgentId);
        if (entry && entry.state === "evicted") return;
        setOperationLoading("Evicting agent...");
        try { await evictAgent(st.selectedAgentId, client); } finally { setOperationLoading(null); }
      },
      "shift+v": async () => {
        const aid = useAgentsStore.getState().selectedAgentId;
        if (!client || !aid) return;
        setOperationLoading("Verifying agent...");
        try { await verifyAgent(aid, client); } finally { setOperationLoading(null); }
      },
      g: () => {
        const st = useAgentsStore.getState();
        if (st.activeTab === "delegations") { setSelectedDelegationIndex(jumpToStart()); }
        else { setSelectedAgentIndex(jumpToStart()); const a = displayAgentIds()[0]; if (a) setSelectedAgentId(a); }
      },
      "shift+g": () => {
        const st = useAgentsStore.getState();
        if (st.activeTab === "delegations") { setSelectedDelegationIndex(jumpToEnd(st.delegations.length)); }
        else { const idx = jumpToEnd(displayAgentIds().length); setSelectedAgentIndex(idx); const a = displayAgentIds()[idx]; if (a) setSelectedAgentId(a); }
      },
      y: () => { const aid = useAgentsStore.getState().selectedAgentId; if (aid) copy(aid); },
      n: () => { useCommandRunnerStore.getState().reset(); executeLocalCommand("agent", ["spawn"]); },
    };
  });

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Main content */}
      <box flexGrow={1} flexDirection="row">
        {/* Left sidebar: agent list (30%) */}
        <box width="30%" height="100%" borderStyle="single" borderColor={uiFocusPane === "left" ? focusColor.activeBorder : focusColor.inactiveBorder} flexDirection="column">
          <box height={1} width="100%">
            {agentsLoading()
              ? <LoadingIndicator message="Agents" centered={false} />
              : <text>{"--- Agents ---"}</text>}
          </box>

          {/* Agents list */}
          {displayAgentIds().length === 0 ? (
            <EmptyState
              message="No agents registered."
              hint="Start an agent with 'nexus agent spawn' or add one with the API."
            />
          ) : (
            <ScrollIndicator selectedIndex={selectedAgentIndex()} totalItems={displayAgentIds().length} visibleItems={20}>
              <scrollbox flexGrow={1} width="100%">
                {displayAgentIds().map((agentId, i) => {
                  const isSelected = i === selectedAgentIndex();
                  const isActive = agentId === selectedAgentId();
                  const prefix = isSelected ? "> " : "  ";
                  const suffix = isActive ? " *" : "";
                  const agentEntry = agents().find((a) => a.agent_id === agentId);
                  const state = agentEntry?.state ?? "";
                  const stateColor = agentStateColor[state] ?? statusColor.dim;
                  return (
                    <box key={agentId} height={1} width="100%">
                      <text>
                        <span>{prefix}</span>
                        <span style={textStyle({ bold: isActive })}>{agentId}</span>
                        {state ? <span style={textStyle({ fg: stateColor })}>{` [${state}]`}</span> : ""}
                        <span style={textStyle({ dim: true })}>{suffix}</span>
                      </text>
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
          <SubTabBar tabs={visibleTabs} activeTab={activeTab()} onSelect={setActiveTab as (id: string) => void} />

          {/* Operation in-progress feedback */}
          {operationLoading() && (
            <box height={1} width="100%">
              <LoadingIndicator message={operationLoading() ?? undefined} centered={false} />
            </box>
          )}

          {/* Error display */}
          {error() && (
            <box height={1} width="100%">
              <StyledText>{`Error: ${error()}`}</StyledText>
            </box>
          )}

          {/* Detail content — use Switch/Match for reactive tab switching */}
          <box flexGrow={1} borderStyle="single">
            <Switch>
              <Match when={activeTab() === "status"}>
                <AgentStatusView
                  status={agentStatus()}
                  spec={agentSpec()}
                  identity={agentIdentity()}
                  loading={statusLoading()}
                  trustScore={trustScore()}
                  reputation={reputation()}
                />
              </Match>
              <Match when={activeTab() === "delegations"}>
                <DelegationList
                  delegations={delegations()}
                  selectedIndex={selectedDelegationIndex()}
                  loading={delegationsLoading()}
                  expandedDelegation={expandedDelegation()}
                />
              </Match>
              <Match when={activeTab() === "inbox"}>
                <InboxView
                  messages={inboxMessages()}
                  count={inboxCount()}
                  processedMessages={useAgentsStore.getState().processedMessages}
                  deadLetterMessages={useAgentsStore.getState().deadLetterMessages}
                  loading={inboxLoading()}
                />
              </Match>
              <Match when={activeTab() === "trajectories"}>
                <TrajectoriesTab
                  trajectories={trajectories()}
                  loading={trajectoriesLoading()}
                />
              </Match>
            </Switch>
          </box>
        </box>
      </box>

      {/* Command runner output (when agent spawn is running) */}
      {useCommandRunnerStore((s) => s.status) !== "idle" && (
        <box borderStyle="single" height={6} width="100%">
          <CommandOutput />
        </box>
      )}

      {/* Help bar */}
      <box height={1} width="100%">
        {copied
          ? <text style={textStyle({ fg: "green" })}>Copied!</text>
          : <text>
          {"j/k:navigate  Tab:switch tab  r:refresh  n:spawn agent  Enter:detail  d:revoke  Shift+W:warmup  Shift+E:evict  y:copy  q:quit"}
        </text>}
      </box>
    </box>
  );
}
