/**
 * Infrastructure & Events panel.
 *
 * Tabbed layout: Events (SSE stream) | Connectors | Subscriptions | Locks | Secrets
 *
 * Press 'f' to enter event type filter mode, 's' to enter search filter mode.
 * In filter mode, type the filter value, Enter to apply, Escape to cancel.
 */

import React, { useState, useEffect, useCallback } from "react";
import { useEventsStore } from "../../stores/events-store.js";
import { useInfraStore } from "../../stores/infra-store.js";
import type { InfraTab } from "../../stores/infra-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { ConnectorList } from "./connector-list.js";
import { SubscriptionList } from "./subscription-list.js";
import { LockList } from "./lock-list.js";
import { SecretsAudit } from "./secrets-audit.js";
import { MclReplay } from "./mcl-replay.js";
import { useKnowledgeStore } from "../../stores/knowledge-store.js";

type FilterMode = "none" | "type" | "search" | "mcl_urn" | "mcl_aspect";

type PanelTab = "events" | "mcl" | InfraTab;

const TAB_ORDER: readonly PanelTab[] = [
  "events",
  "mcl",
  "connectors",
  "subscriptions",
  "locks",
  "secrets",
];

const TAB_LABELS: Readonly<Record<PanelTab, string>> = {
  events: "Events",
  mcl: "MCL",
  connectors: "Connectors",
  subscriptions: "Subscriptions",
  locks: "Locks",
  secrets: "Secrets",
};

export default function EventsPanel(): React.ReactNode {
  const apiClient = useApi();
  const config = useGlobalStore((s) => s.config);

  // Filter input state
  const [filterMode, setFilterMode] = useState<FilterMode>("none");
  const [filterBuffer, setFilterBuffer] = useState("");

  // MCL filter state
  const [mclUrnFilter, setMclUrnFilter] = useState("");
  const [mclAspectFilter, setMclAspectFilter] = useState("");

  // Events store (SSE)
  const connected = useEventsStore((s) => s.connected);
  const events = useEventsStore((s) => s.filteredEvents);
  const reconnectCount = useEventsStore((s) => s.reconnectCount);
  const filters = useEventsStore((s) => s.filters);
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
  const infraError = useInfraStore((s) => s.error);

  const fetchConnectors = useInfraStore((s) => s.fetchConnectors);
  const fetchSubscriptions = useInfraStore((s) => s.fetchSubscriptions);
  const deleteSubscription = useInfraStore((s) => s.deleteSubscription);
  const testSubscription = useInfraStore((s) => s.testSubscription);
  const fetchLocks = useInfraStore((s) => s.fetchLocks);
  const releaseLock = useInfraStore((s) => s.releaseLock);
  const fetchSecretAudit = useInfraStore((s) => s.fetchSecretAudit);
  const setInfraTab = useInfraStore((s) => s.setActiveTab);
  const setSelectedConnectorIndex = useInfraStore((s) => s.setSelectedConnectorIndex);
  const setSelectedSubscriptionIndex = useInfraStore((s) => s.setSelectedSubscriptionIndex);
  const setSelectedLockIndex = useInfraStore((s) => s.setSelectedLockIndex);

  // Track the combined active tab locally
  const [activeTab, setActiveTab] = React.useState<PanelTab>("events");

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

  // Knowledge store (MCL replay)
  const fetchReplay = useKnowledgeStore((s) => s.fetchReplay);
  const clearReplay = useKnowledgeStore((s) => s.clearReplay);

  // Fetch infra data when switching tabs
  useEffect(() => {
    if (!apiClient || activeTab === "events") return;

    if (activeTab === "mcl") void fetchReplay(apiClient, 0, 50);
    else if (activeTab === "connectors") fetchConnectors(apiClient);
    else if (activeTab === "subscriptions") fetchSubscriptions(apiClient);
    else if (activeTab === "locks") fetchLocks(apiClient);
    else if (activeTab === "secrets") fetchSecretAudit(apiClient);
  }, [activeTab, apiClient, fetchConnectors, fetchSubscriptions, fetchLocks, fetchSecretAudit, fetchReplay]);

  // Sync infra tab state
  useEffect(() => {
    if (activeTab !== "events" && activeTab !== "mcl") {
      setInfraTab(activeTab as InfraTab);
    }
  }, [activeTab, setInfraTab]);

  const currentItemCount = (): number => {
    switch (activeTab) {
      case "connectors": return connectors.length;
      case "subscriptions": return subscriptions.length;
      case "locks": return locks.length;
      default: return 0;
    }
  };

  const currentSelectedIndex = (): number => {
    switch (activeTab) {
      case "connectors": return selectedConnectorIndex;
      case "subscriptions": return selectedSubscriptionIndex;
      case "locks": return selectedLockIndex;
      default: return 0;
    }
  };

  const setCurrentSelectedIndex = (index: number): void => {
    switch (activeTab) {
      case "connectors": setSelectedConnectorIndex(index); break;
      case "subscriptions": setSelectedSubscriptionIndex(index); break;
      case "locks": setSelectedLockIndex(index); break;
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
    } else if (apiClient) {
      if (activeTab === "connectors") fetchConnectors(apiClient);
      else if (activeTab === "subscriptions") fetchSubscriptions(apiClient);
      else if (activeTab === "locks") fetchLocks(apiClient);
      else if (activeTab === "secrets") fetchSecretAudit(apiClient);
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
    filterMode !== "none"
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
            const max = currentItemCount() - 1;
            if (max >= 0) setCurrentSelectedIndex(Math.min(currentSelectedIndex() + 1, max));
          },
          down: () => {
            const max = currentItemCount() - 1;
            if (max >= 0) setCurrentSelectedIndex(Math.min(currentSelectedIndex() + 1, max));
          },
          k: () => {
            setCurrentSelectedIndex(Math.max(currentSelectedIndex() - 1, 0));
          },
          up: () => {
            setCurrentSelectedIndex(Math.max(currentSelectedIndex() - 1, 0));
          },
          tab: () => {
            const idx = TAB_ORDER.indexOf(activeTab);
            const next = TAB_ORDER[(idx + 1) % TAB_ORDER.length];
            if (next) setActiveTab(next);
          },
          c: () => clearEvents(),
          r: () => refresh(),
          f: () => {
            if (activeTab === "events") {
              setFilterMode("type");
              setFilterBuffer(filters.eventType ?? "");
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
            }
          },
          d: () => {
            if (activeTab === "subscriptions" && apiClient) {
              const sub = subscriptions[selectedSubscriptionIndex];
              if (sub) deleteSubscription(sub.subscription_id, apiClient);
            } else if (activeTab === "locks" && apiClient) {
              const lock = locks[selectedLockIndex];
              if (lock) releaseLock(lock.resource, lock.lock_id, apiClient);
            }
          },
          t: () => {
            if (activeTab === "subscriptions" && apiClient) {
              const sub = subscriptions[selectedSubscriptionIndex];
              if (sub) testSubscription(sub.subscription_id, apiClient);
            }
          },
        },
    handleUnhandledKey,
  );

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

      {/* Error display */}
      {infraError && activeTab !== "events" && activeTab !== "mcl" && (
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
                  : reconnectCount > 0
                    ? `◐ Reconnecting (attempt ${reconnectCount})...`
                    : "○ Disconnected"}
              </text>
            </box>

            {/* Event stream */}
            <scrollbox flexGrow={1} width="100%">
              {events.length === 0 ? (
                <text>Waiting for events...</text>
              ) : (
                events.map((event, index) => (
                  <box key={event.id ?? index} height={1} width="100%" flexDirection="row">
                    <text>{`[${event.event}] ${truncate(event.data, 120)}`}</text>
                  </box>
                ))
              )}
            </scrollbox>
          </box>
        )}

        {activeTab === "mcl" && <MclReplay urnFilter={mclUrnFilter} aspectFilter={mclAspectFilter} />}

        {activeTab === "connectors" && (
          <ConnectorList
            connectors={connectors}
            selectedIndex={selectedConnectorIndex}
            loading={connectorsLoading}
          />
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
          />
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {activeTab === "events" && filterMode !== "none"
            ? "Type filter, Enter:apply, Escape:cancel, Backspace:delete"
            : activeTab === "events"
            ? "f:filter type  s:filter search  c:clear  r:reconnect  Tab:switch tab  q:quit"
            : activeTab === "mcl" && filterMode !== "none"
              ? "Type filter, Enter:apply, Escape:cancel, Backspace:delete"
              : activeTab === "mcl"
              ? "u:filter URN  n:filter aspect  r:refresh  Tab:switch tab  q:quit"
              : activeTab === "subscriptions"
                ? "j/k:navigate  d:delete  t:test  r:refresh  Tab:switch tab"
                : activeTab === "locks"
                  ? "j/k:navigate  d:release  r:refresh  Tab:switch tab"
                  : "j/k:navigate  r:refresh  Tab:switch tab"}
        </text>
      </box>
    </box>
  );
}

function truncate(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str;
  return str.slice(0, maxLen - 3) + "...";
}
