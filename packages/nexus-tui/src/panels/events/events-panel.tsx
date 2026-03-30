/**
 * Infrastructure & Events panel.
 *
 * Tabbed layout: Events (SSE stream) | Connectors | Subscriptions | Locks | Secrets
 *
 * Press 'f' to enter event type filter mode, 's' to enter search filter mode.
 * In filter mode, type the filter value, Enter to apply, Escape to cancel.
 */

import React, { useState, useEffect, useCallback, useMemo } from "react";
import { useEventsStore } from "../../stores/events-store.js";
import { useInfraStore } from "../../stores/infra-store.js";
import type { InfraTab } from "../../stores/infra-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import { jumpToStart, jumpToEnd } from "../../shared/hooks/use-list-navigation.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useUiStore } from "../../stores/ui-store.js";
import { useVisibleTabs, type TabDef } from "../../shared/hooks/use-visible-tabs.js";
import { ConnectorList } from "./connector-list.js";
import { ConnectorDetail } from "./connector-detail.js";
import { SubscriptionList } from "./subscription-list.js";
import { LockList } from "./lock-list.js";
import { SecretsAudit } from "./secrets-audit.js";
import { MclReplay } from "./mcl-replay.js";
import { EventReplay } from "./event-replay.js";
import { OperationsTab } from "./operations-tab.js";
import { AuditTrail } from "./audit-trail.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { ScrollIndicator } from "../../shared/components/scroll-indicator.js";
import { Tooltip } from "../../shared/components/tooltip.js";
import { SubTabBar } from "../../shared/components/sub-tab-bar.js";
import { subTabForward } from "../../shared/components/sub-tab-bar-utils.js";
import { useTabFallback } from "../../shared/hooks/use-tab-fallback.js";

type FilterMode = "none" | "type" | "search" | "mcl_urn" | "mcl_aspect" | "acquire_path" | "secrets_filter" | "replay_filter";

type PanelTab = "events" | "mcl" | "replay" | "operations" | "audit" | InfraTab;

const ALL_TABS: readonly TabDef<PanelTab>[] = [
  { id: "events", label: "Events", brick: "eventlog" },
  { id: "mcl", label: "MCL", brick: "catalog" },
  { id: "replay", label: "Replay", brick: "eventlog" },
  { id: "operations", label: "Operations", brick: "eventlog" },
  { id: "connectors", label: "Connectors", brick: null },
  { id: "subscriptions", label: "Subscriptions", brick: "eventlog" },
  { id: "locks", label: "Locks", brick: null },
  { id: "secrets", label: "Secrets", brick: "auth" },
  { id: "audit", label: "Audit", brick: "auth" },
];


export default function EventsPanel(): React.ReactNode {
  const apiClient = useApi();
  const confirm = useConfirmStore((s) => s.confirm);
  const overlayActive = useUiStore((s) => s.overlayActive);
  const visibleTabs = useVisibleTabs(ALL_TABS);
  const config = useGlobalStore((s) => s.config);

  // Clipboard copy
  const { copy, copied } = useCopy();

  // Filter input state
  const [filterMode, setFilterMode] = useState<FilterMode>("none");
  const [filterBuffer, setFilterBuffer] = useState("");

  // Event detail expansion
  const [selectedEventIndex, setSelectedEventIndex] = useState(-1);
  const [expandedEventIndex, setExpandedEventIndex] = useState<number | null>(null);

  // MCL filter state
  const [mclUrnFilter, setMclUrnFilter] = useState("");
  const [mclAspectFilter, setMclAspectFilter] = useState("");

  // Secrets filter state
  const [secretsFilter, setSecretsFilter] = useState("");

  // Replay filter state
  const [replayTypeFilter, setReplayTypeFilter] = useState("");

  // Connector detail state
  const [connectorDetailView, setConnectorDetailView] = useState(false);

  // Audit selected index
  const [selectedAuditIndex, setSelectedAuditIndex] = useState(0);

  // Events store (SSE)
  const connected = useEventsStore((s) => s.connected);
  const events = useEventsStore((s) => s.filteredEvents);
  const reconnectCount = useEventsStore((s) => s.reconnectCount);
  const reconnectExhausted = useEventsStore((s) => s.reconnectExhausted);
  const filters = useEventsStore((s) => s.filters);
  const eventsOverflowed = useEventsStore((s) => s.eventsOverflowed);
  const evictedCount = useEventsStore((s) => s.evictedCount);
  const eventsBuffer = useEventsStore((s) => s.eventsBuffer);
  const connect = useEventsStore((s) => s.connect);
  const disconnect = useEventsStore((s) => s.disconnect);
  const clearEvents = useEventsStore((s) => s.clearEvents);
  const setFilter = useEventsStore((s) => s.setFilter);

  // Infra store
  const infraTab = useInfraStore((s) => s.activeTab);
  const connectors = useInfraStore((s) => s.connectors);
  const connectorsLoading = useInfraStore((s) => s.connectorsLoading);
  const selectedConnectorIndex = useInfraStore((s) => s.selectedConnectorIndex);
  const subscriptions = useInfraStore((s) => s.subscriptions);
  const subscriptionsLoading = useInfraStore((s) => s.subscriptionsLoading);
  const selectedSubscriptionIndex = useInfraStore((s) => s.selectedSubscriptionIndex);
  const locks = useInfraStore((s) => s.locks);
  const locksLoading = useInfraStore((s) => s.locksLoading);
  const selectedLockIndex = useInfraStore((s) => s.selectedLockIndex);
  const secretAuditEntries = useInfraStore((s) => s.secretAuditEntries);
  const secretsLoading = useInfraStore((s) => s.secretsLoading);
  const operations = useInfraStore((s) => s.operations);
  const operationsLoading = useInfraStore((s) => s.operationsLoading);
  const selectedOperationIndex = useInfraStore((s) => s.selectedOperationIndex);
  const infraError = useInfraStore((s) => s.error);

  const connectorCapabilities = useInfraStore((s) => s.connectorCapabilities);
  const capabilitiesLoading = useInfraStore((s) => s.capabilitiesLoading);
  const auditTransactions = useInfraStore((s) => s.auditTransactions);
  const auditLoading = useInfraStore((s) => s.auditLoading);
  const auditHasMore = useInfraStore((s) => s.auditHasMore);
  const auditNextCursor = useInfraStore((s) => s.auditNextCursor);

  const fetchConnectors = useInfraStore((s) => s.fetchConnectors);
  const fetchSubscriptions = useInfraStore((s) => s.fetchSubscriptions);
  const deleteSubscription = useInfraStore((s) => s.deleteSubscription);
  const testSubscription = useInfraStore((s) => s.testSubscription);
  const fetchLocks = useInfraStore((s) => s.fetchLocks);
  const acquireLock = useInfraStore((s) => s.acquireLock);
  const releaseLock = useInfraStore((s) => s.releaseLock);
  const extendLock = useInfraStore((s) => s.extendLock);
  const fetchSecretAudit = useInfraStore((s) => s.fetchSecretAudit);
  const fetchOperations = useInfraStore((s) => s.fetchOperations);
  const fetchConnectorCapabilities = useInfraStore((s) => s.fetchConnectorCapabilities);
  const fetchAuditTransactions = useInfraStore((s) => s.fetchAuditTransactions);
  const setSelectedOperationIndex = useInfraStore((s) => s.setSelectedOperationIndex);
  const setInfraTab = useInfraStore((s) => s.setActiveTab);
  const setSelectedConnectorIndex = useInfraStore((s) => s.setSelectedConnectorIndex);
  const setSelectedSubscriptionIndex = useInfraStore((s) => s.setSelectedSubscriptionIndex);
  const setSelectedLockIndex = useInfraStore((s) => s.setSelectedLockIndex);

  // Track the combined active tab locally
  const [activeTab, setActiveTab] = React.useState<PanelTab>("events");

  useTabFallback(visibleTabs, activeTab, setActiveTab);

  // Reset expanded event when events change (index may become stale after SSE adds/evicts)
  const eventsLength = events.length;
  useEffect(() => {
    setExpandedEventIndex(null);
  }, [eventsLength]);

  // Auto-connect SSE on mount, reconnect when identity changes
  useEffect(() => {
    if (config.apiKey && config.baseUrl) {
      connect(config.baseUrl, config.apiKey, {
        agentId: config.agentId,
        subject: config.subject,
        zoneId: config.zoneId,
      });
    }
    return () => disconnect();
  }, [config.apiKey, config.baseUrl, config.agentId, config.subject, config.zoneId, connect, disconnect]);

  // Knowledge store (MCL replay + event replay)
  const fetchReplay = useKnowledgeStore((s) => s.fetchReplay);
  const clearReplay = useKnowledgeStore((s) => s.clearReplay);
  const fetchEventReplay = useKnowledgeStore((s) => s.fetchEventReplay);
  const clearEventReplay = useKnowledgeStore((s) => s.clearEventReplay);

  // Fetch infra data when switching tabs
  useEffect(() => {
    if (!apiClient || activeTab === "events") return;

    if (activeTab === "mcl") void fetchReplay(apiClient, 0, 50);
    else if (activeTab === "replay") void fetchEventReplay({}, apiClient);
    else if (activeTab === "connectors") { fetchConnectors(apiClient); setConnectorDetailView(false); }
    else if (activeTab === "subscriptions") fetchSubscriptions(apiClient);
    else if (activeTab === "locks") fetchLocks(apiClient);
    else if (activeTab === "secrets") fetchSecretAudit(apiClient);
    else if (activeTab === "operations") fetchOperations(apiClient);
    else if (activeTab === "audit") void fetchAuditTransactions({}, apiClient);
  }, [activeTab, apiClient, fetchConnectors, fetchSubscriptions, fetchLocks, fetchSecretAudit, fetchOperations, fetchReplay, fetchEventReplay, fetchAuditTransactions]);

  // Sync infra tab state
  useEffect(() => {
    if (activeTab !== "events" && activeTab !== "mcl" && activeTab !== "replay" && activeTab !== "operations" && activeTab !== "audit") {
      setInfraTab(activeTab as InfraTab);
    }
  }, [activeTab, setInfraTab]);

  const currentItemCount = (): number => {
    switch (activeTab) {
      case "events": return events.length;
      case "connectors": return connectors.length;
      case "subscriptions": return subscriptions.length;
      case "locks": return locks.length;
      case "operations": return operations.length;
      case "audit": return auditTransactions.length;
      default: return 0;
    }
  };

  const currentSelectedIndex = (): number => {
    switch (activeTab) {
      case "connectors": return selectedConnectorIndex;
      case "subscriptions": return selectedSubscriptionIndex;
      case "locks": return selectedLockIndex;
      case "operations": return selectedOperationIndex;
      case "audit": return selectedAuditIndex;
      default: return 0;
    }
  };

  const setCurrentSelectedIndex = (index: number): void => {
    switch (activeTab) {
      case "events": setSelectedEventIndex(index); break;
      case "connectors": setSelectedConnectorIndex(index); break;
      case "subscriptions": setSelectedSubscriptionIndex(index); break;
      case "locks": setSelectedLockIndex(index); break;
      case "operations": setSelectedOperationIndex(index); break;
      case "audit": setSelectedAuditIndex(index); break;
    }
  };

  const refresh = (): void => {
    if (activeTab === "events") {
      if (config.apiKey && config.baseUrl) {
        disconnect();
        connect(config.baseUrl, config.apiKey, {
          agentId: config.agentId,
          subject: config.subject,
          zoneId: config.zoneId,
        });
      }
    } else if (activeTab === "mcl" && apiClient) {
      clearReplay();
      void fetchReplay(apiClient, 0, 50);
    } else if (activeTab === "replay" && apiClient) {
      clearEventReplay();
      void fetchEventReplay({ event_types: replayTypeFilter || undefined }, apiClient);
    } else if (apiClient) {
      if (activeTab === "connectors") { fetchConnectors(apiClient); setConnectorDetailView(false); }
      else if (activeTab === "subscriptions") fetchSubscriptions(apiClient);
      else if (activeTab === "locks") fetchLocks(apiClient);
      else if (activeTab === "secrets") fetchSecretAudit(apiClient);
      else if (activeTab === "operations") fetchOperations(apiClient);
      else if (activeTab === "audit") void fetchAuditTransactions({}, apiClient);
    }
  };

  // Handle unhandled keys in filter input mode
  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      if (filterMode === "none") return;
      if (keyName.length === 1) {
        setFilterBuffer((b) => b + keyName);
      } else if (keyName === "space") {
        setFilterBuffer((b) => b + " ");
      }
    },
    [filterMode],
  );

  useKeyboard(
    overlayActive
      ? {}
      : filterMode !== "none"
      ? {
          // Filter input mode: capture keystrokes
          return: () => {
            const value = filterBuffer.trim() || "";
            if (filterMode === "type") {
              setFilter({ eventType: value || null });
            } else if (filterMode === "search") {
              setFilter({ search: value || null });
            } else if (filterMode === "mcl_urn") {
              setMclUrnFilter(value);
            } else if (filterMode === "mcl_aspect") {
              setMclAspectFilter(value);
            } else if (filterMode === "acquire_path") {
              if (value && apiClient) {
                acquireLock(value, "mutex", 60, apiClient);
              }
            } else if (filterMode === "secrets_filter") {
              setSecretsFilter(value);
            } else if (filterMode === "replay_filter") {
              setReplayTypeFilter(value);
              if (apiClient) void fetchEventReplay({ event_types: value || undefined }, apiClient);
            }
            setFilterMode("none");
            setFilterBuffer("");
          },
          escape: () => {
            setFilterMode("none");
            setFilterBuffer("");
          },
          backspace: () => {
            setFilterBuffer((b) => b.slice(0, -1));
          },
        }
      : {
          // Normal mode
          j: () => {
            if (activeTab === "events") {
              setSelectedEventIndex((i) => Math.min(i + 1, events.length - 1));
            } else {
              const max = currentItemCount() - 1;
              if (max >= 0) setCurrentSelectedIndex(Math.min(currentSelectedIndex() + 1, max));
            }
          },
          down: () => {
            if (activeTab === "events") {
              setSelectedEventIndex((i) => Math.min(i + 1, events.length - 1));
            } else {
              const max = currentItemCount() - 1;
              if (max >= 0) setCurrentSelectedIndex(Math.min(currentSelectedIndex() + 1, max));
            }
          },
          k: () => {
            if (activeTab === "events") {
              setSelectedEventIndex((i) => Math.max(i - 1, 0));
            } else {
              setCurrentSelectedIndex(Math.max(currentSelectedIndex() - 1, 0));
            }
          },
          up: () => {
            if (activeTab === "events") {
              setSelectedEventIndex((i) => Math.max(i - 1, 0));
            } else {
              setCurrentSelectedIndex(Math.max(currentSelectedIndex() - 1, 0));
            }
          },
          return: () => {
            if (activeTab === "events" && selectedEventIndex >= 0 && selectedEventIndex < events.length) {
              setExpandedEventIndex((prev) => prev === selectedEventIndex ? null : selectedEventIndex);
            } else if (activeTab === "connectors" && apiClient) {
              const conn = connectors[selectedConnectorIndex];
              if (conn) {
                void fetchConnectorCapabilities(conn.name, apiClient);
                setConnectorDetailView(true);
              }
            }
          },
          escape: () => {
            if (activeTab === "events" && expandedEventIndex !== null) {
              setExpandedEventIndex(null);
            } else if (activeTab === "connectors" && connectorDetailView) {
              setConnectorDetailView(false);
            }
          },
          tab: () => subTabForward(visibleTabs, activeTab, setActiveTab),
          c: () => clearEvents(),
          r: () => refresh(),
          f: () => {
            if (activeTab === "events") {
              setFilterMode("type");
              setFilterBuffer(filters.eventType ?? "");
            } else if (activeTab === "replay") {
              setFilterMode("replay_filter");
              setFilterBuffer(replayTypeFilter);
            }
          },
          m: () => {
            if (activeTab === "audit" && auditHasMore && auditNextCursor && apiClient) {
              void fetchAuditTransactions({ cursor: auditNextCursor }, apiClient);
            }
          },
          s: () => {
            if (activeTab === "events") {
              setFilterMode("search");
              setFilterBuffer(filters.search ?? "");
            }
          },
          u: () => {
            if (activeTab === "mcl") {
              setFilterMode("mcl_urn");
              setFilterBuffer(mclUrnFilter);
            }
          },
          n: () => {
            if (activeTab === "mcl") {
              setFilterMode("mcl_aspect");
              setFilterBuffer(mclAspectFilter);
            } else if (activeTab === "locks") {
              setFilterMode("acquire_path");
              setFilterBuffer("");
            }
          },
          d: async () => {
            if (activeTab === "subscriptions" && apiClient) {
              const sub = subscriptions[selectedSubscriptionIndex];
              if (sub) {
                const ok = await confirm("Delete subscription?", "Delete this event subscription.");
                if (!ok) return;
                deleteSubscription(sub.subscription_id, apiClient);
              }
            } else if (activeTab === "locks" && apiClient) {
              const lock = locks[selectedLockIndex];
              if (lock) {
                const ok = await confirm("Release lock?", "Release this lock. Other waiters may acquire it.");
                if (!ok) return;
                releaseLock(lock.resource, lock.lock_id, apiClient);
              }
            }
          },
          t: () => {
            if (activeTab === "subscriptions" && apiClient) {
              const sub = subscriptions[selectedSubscriptionIndex];
              if (sub) testSubscription(sub.subscription_id, apiClient);
            }
          },
          e: () => {
            if (activeTab === "locks" && apiClient) {
              const lock = locks[selectedLockIndex];
              if (lock) extendLock(lock.resource, lock.lock_id, 60, apiClient);
            }
          },
          "/": () => {
            if (activeTab === "secrets") {
              setFilterMode("secrets_filter");
              setFilterBuffer(secretsFilter);
            }
          },
          y: () => {
            if (activeTab === "events") {
              // Copy the currently selected event (or latest if none selected)
              const idx = selectedEventIndex >= 0 ? selectedEventIndex : events.length - 1;
              const event = events[idx];
              if (event) copy(event.data);
            }
          },
          g: () => {
            setCurrentSelectedIndex(jumpToStart());
          },
          "shift+g": () => {
            setCurrentSelectedIndex(jumpToEnd(currentItemCount()));
          },
        },
    overlayActive ? undefined : handleUnhandledKey,
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <Tooltip tooltipKey="events-panel" message="Tip: Press ? for keybinding help" />
      {/* Tab bar */}
      <SubTabBar tabs={visibleTabs} activeTab={activeTab} />

      {/* Filter bar (events tab) */}
      {activeTab === "events" && (
        <box height={1} width="100%">
          <text>
            {filterMode === "type"
              ? `Filter type: ${filterBuffer}\u2588`
              : filterMode === "search"
                ? `Filter search: ${filterBuffer}\u2588`
                : `Filter: type=${filters.eventType ?? "*"} search=${filters.search ?? "*"}`}
          </text>
        </box>
      )}

      {/* Filter bar (MCL tab) */}
      {activeTab === "mcl" && (
        <box height={1} width="100%">
          <text>
            {filterMode === "mcl_urn"
              ? `Filter URN: ${filterBuffer}\u2588`
              : filterMode === "mcl_aspect"
                ? `Filter aspect: ${filterBuffer}\u2588`
                : `Filter: URN=${mclUrnFilter || "*"} aspect=${mclAspectFilter || "*"}`}
          </text>
        </box>
      )}

      {/* Acquire lock input bar (locks tab) */}
      {activeTab === "locks" && filterMode === "acquire_path" && (
        <box height={1} width="100%">
          <text>{`Acquire lock path: ${filterBuffer}\u2588`}</text>
        </box>
      )}

      {/* Replay filter bar */}
      {activeTab === "replay" && (
        <box height={1} width="100%">
          <text>
            {filterMode === "replay_filter"
              ? `Filter event type: ${filterBuffer}\u2588`
              : `Filter: event_type=${replayTypeFilter || "*"}`}
          </text>
        </box>
      )}

      {/* Secrets filter bar */}
      {activeTab === "secrets" && (
        <box height={1} width="100%">
          <text>
            {filterMode === "secrets_filter"
              ? `Filter: ${filterBuffer}\u2588`
              : secretsFilter
                ? `Filter: ${secretsFilter}`
                : ""}
          </text>
        </box>
      )}

      {/* Error display */}
      {infraError && activeTab !== "events" && activeTab !== "mcl" && activeTab !== "replay" && (
        <box height={1} width="100%">
          <text>{`Error: ${infraError}`}</text>
        </box>
      )}

      {/* Main content */}
      <box flexGrow={1} width="100%" borderStyle="single">
        {activeTab === "events" && (
          <box height="100%" width="100%" flexDirection="column">
            {/* SSE status */}
            <box height={1} width="100%">
              <text>
                {connected
                  ? `● Connected — ${events.length} events`
                  : reconnectExhausted
                    ? `✕ Reconnect failed after ${reconnectCount} attempts — press r to retry`
                    : reconnectCount > 0
                      ? `◐ Auto-reconnecting (attempt ${reconnectCount}/10)...`
                      : "○ Disconnected"}
              </text>
            </box>

            {/* Overflow indicator */}
            {eventsOverflowed && (
              <box height={1} width="100%">
                <text dimColor>
                  {`Showing latest ${eventsBuffer.size} of ${eventsBuffer.totalAdded} events (${evictedCount} evicted)`}
                </text>
              </box>
            )}

            {/* Event stream */}
            {expandedEventIndex !== null && expandedEventIndex < events.length ? (
              <box flexGrow={1} width="100%" flexDirection="column">
                <box height={1} width="100%">
                  <text bold>{`[${events[expandedEventIndex]!.event}] — Event #${expandedEventIndex} (Escape to close)`}</text>
                </box>
                <scrollbox flexGrow={1} width="100%">
                  <text>{formatEventData(events[expandedEventIndex]!.data)}</text>
                </scrollbox>
              </box>
            ) : (
              <ScrollIndicator selectedIndex={selectedEventIndex >= 0 ? selectedEventIndex : events.length - 1} totalItems={events.length} visibleItems={20}>
                <scrollbox flexGrow={1} width="100%">
                  {events.length === 0 ? (
                    <EmptyState
                      message="Listening for events..."
                      hint="Waiting for activity on the server."
                    />
                  ) : (
                    events.map((event, index) => (
                      <box key={event.id ?? index} height={1} width="100%" flexDirection="row">
                        <text inverse={index === selectedEventIndex || undefined}>
                          {`[${event.event}] ${event.data}`}
                        </text>
                      </box>
                    ))
                  )}
                </scrollbox>
              </ScrollIndicator>
            )}
          </box>
        )}

        {activeTab === "mcl" && <MclReplay urnFilter={mclUrnFilter} aspectFilter={mclAspectFilter} />}

        {activeTab === "replay" && <EventReplay typeFilter={replayTypeFilter} />}

        {activeTab === "connectors" && (
          connectorDetailView && connectors[selectedConnectorIndex] ? (
            <ConnectorDetail
              connectorName={connectors[selectedConnectorIndex]!.name}
              capabilities={connectorCapabilities}
              loading={capabilitiesLoading}
            />
          ) : (
            <ConnectorList
              connectors={connectors}
              selectedIndex={selectedConnectorIndex}
              loading={connectorsLoading}
            />
          )
        )}

        {activeTab === "subscriptions" && (
          <SubscriptionList
            subscriptions={subscriptions}
            selectedIndex={selectedSubscriptionIndex}
            loading={subscriptionsLoading}
          />
        )}

        {activeTab === "locks" && (
          <LockList
            locks={locks}
            selectedIndex={selectedLockIndex}
            loading={locksLoading}
          />
        )}

        {activeTab === "secrets" && (
          <SecretsAudit
            entries={secretAuditEntries}
            loading={secretsLoading}
            filter={secretsFilter}
          />
        )}

        {activeTab === "operations" && (
          <OperationsTab
            operations={operations}
            selectedIndex={selectedOperationIndex}
            loading={operationsLoading}
          />
        )}

        {activeTab === "audit" && (
          <AuditTrail
            transactions={auditTransactions}
            loading={auditLoading}
            hasMore={auditHasMore}
            selectedIndex={selectedAuditIndex}
          />
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        {copied
          ? <text foregroundColor="green">Copied!</text>
          : <text>
          {filterMode !== "none"
            ? "Type value, Enter:apply, Escape:cancel, Backspace:delete"
            : activeTab === "events"
            ? "j/k:navigate  Enter:expand  f:filter type  s:search  c:clear  r:reconnect  y:copy  Tab:switch"
            : activeTab === "mcl"
              ? "u:filter URN  n:filter aspect  r:refresh  Tab:switch tab"
              : activeTab === "replay"
                ? "f:filter event type  r:refresh  Tab:switch tab"
                : activeTab === "connectors"
                  ? connectorDetailView
                    ? "Escape:back  r:refresh  Tab:switch tab"
                    : "j/k:navigate  Enter:capabilities  r:refresh  Tab:switch tab"
                  : activeTab === "subscriptions"
                    ? "j/k:navigate  d:delete  t:test  r:refresh  Tab:switch tab"
                    : activeTab === "locks"
                      ? "j/k:navigate  n:acquire  d:release  e:extend  r:refresh  Tab:switch tab"
                      : activeTab === "secrets"
                        ? "/:filter  r:refresh  Tab:switch tab"
                        : activeTab === "audit"
                          ? "j/k:navigate  m:load more  r:refresh  Tab:switch tab"
                          : "j/k:navigate  r:refresh  Tab:switch tab"}
        </text>}
      </box>
    </box>
  );
}

function formatEventData(data: string): string {
  try {
    const parsed = JSON.parse(data);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return data;
  }
}
