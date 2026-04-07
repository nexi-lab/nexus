/**
 * Infrastructure & Events panel.
 *
 * Tabbed layout: Events (SSE stream) | Connectors | Subscriptions | Locks | Secrets
 *
 * Press 'f' to enter event type filter mode, 's' to enter search filter mode.
 * In filter mode, type the filter value, Enter to apply, Escape to cancel.
 */

import { createSignal, createEffect, Show } from "solid-js";
import type { JSX } from "solid-js";
import { useEventsStore } from "../../stores/events-store.js";
import { useSseBus } from "../../stores/sse-bus.js";
import { useInfraStore } from "../../stores/infra-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useUiStore } from "../../stores/ui-store.js";
import { useVisibleTabs } from "../../shared/hooks/use-visible-tabs.js";
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
import { useTabFallback } from "../../shared/hooks/use-tab-fallback.js";
import { EVENTS_TABS } from "../../shared/navigation.js";
import { statusColor } from "../../shared/theme.js";
import {
  type FilterMode,
  type EventsBindingContext,
  getEventsKeyBindings,
  getEventsHelpText,
  handleEventsUnhandledKey,
  formatEventData,
} from "./events-panel-keybindings.js";

export default function EventsPanel(): JSX.Element {
  const apiClient = useApi();

  // ---- Reactive store accessors (direct reads via jsx:preserve) ----
  const confirm = useConfirmStore((s) => s.confirm);
  const overlayActive = () => useUiStore((s) => s.overlayActive);
  const visibleTabs = useVisibleTabs(EVENTS_TABS);
  // Clipboard copy
  const { copy, copied } = useCopy();

  // Filter input state
  const [filterMode, setFilterMode] = createSignal<FilterMode>("none");
  const [filterBuffer, setFilterBuffer] = createSignal("");

  // Event detail expansion
  const [selectedEventIndex, setSelectedEventIndex] = createSignal(-1);
  const [expandedEventIndex, setExpandedEventIndex] = createSignal<number | null>(null);

  // MCL filter state
  const [mclUrnFilter, setMclUrnFilter] = createSignal("");
  const [mclAspectFilter, setMclAspectFilter] = createSignal("");

  // Secrets filter state
  const [secretsFilter, setSecretsFilter] = createSignal("");

  // Replay filter state
  const [replayTypeFilter, setReplayTypeFilter] = createSignal("");

  // Connector detail state
  const [connectorDetailView, setConnectorDetailView] = createSignal(false);

  // Audit selected index
  const [selectedAuditIndex, setSelectedAuditIndex] = createSignal(0);

  // ---- Reactive store values (direct reads via jsx:preserve) ----
  const connected = () => useSseBus((s) => s.connected);
  const events = () => useEventsStore((s) => s.filteredEvents);
  const reconnectCount = () => useSseBus((s) => s.reconnectCount);
  const reconnectExhausted = () => useSseBus((s) => s.reconnectExhausted);
  const filters = () => useEventsStore((s) => s.filters);
  const eventsOverflowed = () => useEventsStore((s) => s.eventsOverflowed);
  const evictedCount = () => useEventsStore((s) => s.evictedCount);
  const eventsBuffer = () => useEventsStore((s) => s.eventsBuffer);

  // Actions (stable function references)
  const reconnect = useSseBus((s) => s.reconnect);
  const clearEvents = useEventsStore((s) => s.clearEvents);
  const setFilter = useEventsStore((s) => s.setFilter);

  const activeTab = () => useInfraStore((s) => s.activePanelTab);
  const setActiveTab = useInfraStore((s) => s.setActivePanelTab);
  const connectors = () => useInfraStore((s) => s.connectors);
  const connectorsLoading = () => useInfraStore((s) => s.connectorsLoading);
  const selectedConnectorIndex = () => useInfraStore((s) => s.selectedConnectorIndex);
  const subscriptions = () => useInfraStore((s) => s.subscriptions);
  const subscriptionsLoading = () => useInfraStore((s) => s.subscriptionsLoading);
  const selectedSubscriptionIndex = () => useInfraStore((s) => s.selectedSubscriptionIndex);
  const locks = () => useInfraStore((s) => s.locks);
  const locksLoading = () => useInfraStore((s) => s.locksLoading);
  const selectedLockIndex = () => useInfraStore((s) => s.selectedLockIndex);
  const secretAuditEntries = () => useInfraStore((s) => s.secretAuditEntries);
  const secretsLoading = () => useInfraStore((s) => s.secretsLoading);
  const operations = () => useInfraStore((s) => s.operations);
  const operationsLoading = () => useInfraStore((s) => s.operationsLoading);
  const selectedOperationIndex = () => useInfraStore((s) => s.selectedOperationIndex);
  const infraError = () => useInfraStore((s) => s.error);
  const connectorCapabilities = () => useInfraStore((s) => s.connectorCapabilities);
  const capabilitiesLoading = () => useInfraStore((s) => s.capabilitiesLoading);
  const auditTransactions = () => useInfraStore((s) => s.auditTransactions);
  const auditLoading = () => useInfraStore((s) => s.auditLoading);
  const auditHasMore = () => useInfraStore((s) => s.auditHasMore);
  const auditNextCursor = () => useInfraStore((s) => s.auditNextCursor);

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
  const setSelectedConnectorIndex = useInfraStore((s) => s.setSelectedConnectorIndex);
  const setSelectedSubscriptionIndex = useInfraStore((s) => s.setSelectedSubscriptionIndex);
  const setSelectedLockIndex = useInfraStore((s) => s.setSelectedLockIndex);

  useTabFallback(visibleTabs, activeTab(), setActiveTab);

  // Reset expanded event when events change (index may become stale after SSE adds/evicts)
  createEffect(() => {
    void events().length;
    setExpandedEventIndex(null);
  });

  // SSE connection is managed at the app level by the SSE bus (sse-bus.ts).
  // No per-panel connect/disconnect needed.

  // Knowledge store (MCL replay + event replay)
  const fetchReplay = useKnowledgeStore((s) => s.fetchReplay);
  const clearReplay = useKnowledgeStore((s) => s.clearReplay);
  const fetchEventReplay = useKnowledgeStore((s) => s.fetchEventReplay);
  const clearEventReplay = useKnowledgeStore((s) => s.clearEventReplay);

  // Fetch infra data when switching tabs
  createEffect(() => {
    const tab = activeTab();
    if (!apiClient || tab === "events") return;

    if (tab === "mcl") void fetchReplay(apiClient, 0, 50);
    else if (tab === "replay") void fetchEventReplay({}, apiClient);
    else if (tab === "connectors") { fetchConnectors(apiClient); setConnectorDetailView(false); }
    else if (tab === "subscriptions") fetchSubscriptions(apiClient);
    else if (tab === "locks") fetchLocks(apiClient);
    else if (tab === "secrets") fetchSecretAudit(apiClient);
    else if (tab === "operations") fetchOperations(apiClient);
    else if (tab === "audit") void fetchAuditTransactions({}, apiClient);
  });

  // Handle unhandled keys in filter input mode
  const handleUnhandledKey = (keyName: string) => handleEventsUnhandledKey(filterMode(), setFilterBuffer, keyName);

  useKeyboard(
    (): Record<string, () => void> => {
      // Build binding context fresh each time for reactive reads
      const bindingCtx: EventsBindingContext = {
        activeTab: activeTab(), visibleTabs, setActiveTab,
        filterMode: filterMode(), filterBuffer: filterBuffer(), setFilterMode, setFilterBuffer,
        events: events(), selectedEventIndex: selectedEventIndex(), setSelectedEventIndex,
        expandedEventIndex: expandedEventIndex(), setExpandedEventIndex,
        filters: filters(), setFilter, clearEvents, copy,
        reconnect,
        mclUrnFilter: mclUrnFilter(), setMclUrnFilter, mclAspectFilter: mclAspectFilter(), setMclAspectFilter,
        clearReplay, fetchReplay,
        replayTypeFilter: replayTypeFilter(), setReplayTypeFilter, clearEventReplay, fetchEventReplay,
        connectors: connectors(), selectedConnectorIndex: selectedConnectorIndex(), setSelectedConnectorIndex,
        connectorDetailView: connectorDetailView(), setConnectorDetailView,
        fetchConnectors, fetchConnectorCapabilities,
        subscriptions: subscriptions(), selectedSubscriptionIndex: selectedSubscriptionIndex(), setSelectedSubscriptionIndex,
        deleteSubscription, testSubscription, fetchSubscriptions,
        locks: locks(), selectedLockIndex: selectedLockIndex(), setSelectedLockIndex,
        acquireLock, releaseLock, extendLock, fetchLocks,
        secretsFilter: secretsFilter(), setSecretsFilter, fetchSecretAudit,
        operations: operations(), selectedOperationIndex: selectedOperationIndex(), setSelectedOperationIndex, fetchOperations,
        auditTransactions: auditTransactions(), selectedAuditIndex: selectedAuditIndex(), setSelectedAuditIndex,
        auditHasMore: auditHasMore(), auditNextCursor: auditNextCursor(), fetchAuditTransactions,
        apiClient, confirm,
      };
      return getEventsKeyBindings(overlayActive(), bindingCtx);
    },
    () => overlayActive() ? undefined : handleUnhandledKey,
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <Tooltip tooltipKey="events-panel" message="Tip: Press ? for keybinding help" />
      {/* Tab bar */}
      <SubTabBar tabs={visibleTabs} activeTab={activeTab()} onSelect={setActiveTab as (id: string) => void} />

      {/* Filter bar (events tab) */}
      <Show when={activeTab() === "events"}>
        <box height={1} width="100%">
          <text>
            {filterMode() === "type"
              ? `Filter type: ${filterBuffer()}\u2588`
              : filterMode() === "search"
                ? `Filter search: ${filterBuffer()}\u2588`
                : `Filter: type=${filters().eventType ?? "*"} search=${filters().search ?? "*"}`}
          </text>
        </box>
      </Show>

      {/* Filter bar (MCL tab) */}
      <Show when={activeTab() === "mcl"}>
        <box height={1} width="100%">
          <text>
            {filterMode() === "mcl_urn"
              ? `Filter URN: ${filterBuffer()}\u2588`
              : filterMode() === "mcl_aspect"
                ? `Filter aspect: ${filterBuffer()}\u2588`
                : `Filter: URN=${mclUrnFilter() || "*"} aspect=${mclAspectFilter() || "*"}`}
          </text>
        </box>
      </Show>

      {/* Acquire lock input bar (locks tab) */}
      <Show when={activeTab() === "locks" && filterMode() === "acquire_path"}>
        <box height={1} width="100%">
          <text>{`Acquire lock path: ${filterBuffer()}\u2588`}</text>
        </box>
      </Show>

      {/* Replay filter bar */}
      <Show when={activeTab() === "replay"}>
        <box height={1} width="100%">
          <text>
            {filterMode() === "replay_filter"
              ? `Filter event type: ${filterBuffer()}\u2588`
              : `Filter: event_type=${replayTypeFilter() || "*"}`}
          </text>
        </box>
      </Show>

      {/* Secrets filter bar */}
      <Show when={activeTab() === "secrets"}>
        <box height={1} width="100%">
          <text>
            {filterMode() === "secrets_filter"
              ? `Filter: ${filterBuffer()}\u2588`
              : secretsFilter()
                ? `Filter: ${secretsFilter()}`
                : ""}
          </text>
        </box>
      </Show>

      {/* Error display */}
      <Show when={infraError() && activeTab() !== "events" && activeTab() !== "mcl" && activeTab() !== "replay"}>
        <box height={1} width="100%">
          <text>{`Error: ${infraError()}`}</text>
        </box>
      </Show>

      {/* Main content */}
      <box flexGrow={1} width="100%" borderStyle="single">
        <Show when={activeTab() === "events"}>
          <box height="100%" width="100%" flexDirection="column">
            {/* SSE status */}
            <box height={1} width="100%">
              <text>
                {connected()
                  ? `● Connected — ${events().length} events`
                  : reconnectExhausted()
                    ? `✕ Reconnect failed after ${reconnectCount()} attempts — press r to retry`
                    : reconnectCount() > 0
                      ? `◐ Auto-reconnecting (attempt ${reconnectCount()}/10)...`
                      : "○ Disconnected"}
              </text>
            </box>

            {/* Overflow indicator */}
            <Show when={eventsOverflowed()}>
              <box height={1} width="100%">
                <text dimColor>
                  {`Showing latest ${eventsBuffer().size} of ${eventsBuffer().totalAdded} events (${evictedCount()} evicted)`}
                </text>
              </box>
            </Show>

            {/* Event stream */}
            <Show
              when={expandedEventIndex() !== null && expandedEventIndex()! < events().length}
              fallback={
                <ScrollIndicator selectedIndex={selectedEventIndex() >= 0 ? selectedEventIndex() : events().length - 1} totalItems={events().length} visibleItems={20}>
                  <scrollbox flexGrow={1} width="100%">
                    <Show
                      when={events().length > 0}
                      fallback={
                        <EmptyState
                          message="Listening for events..."
                          hint="Waiting for activity on the server."
                        />
                      }
                    >
                      {events().map((event, index) => (
                        <box height={1} width="100%" flexDirection="row">
                          <text inverse={index === selectedEventIndex() || undefined}>
                            {`[${event.event}] ${event.data}`}
                          </text>
                        </box>
                      ))}
                    </Show>
                  </scrollbox>
                </ScrollIndicator>
              }
            >
              <box flexGrow={1} width="100%" flexDirection="column">
                <box height={1} width="100%">
                  <text bold>{`[${events()[expandedEventIndex()!]!.event}] — Event #${expandedEventIndex()} (Escape to close)`}</text>
                </box>
                <scrollbox flexGrow={1} width="100%">
                  <text>{formatEventData(events()[expandedEventIndex()!]!.data)}</text>
                </scrollbox>
              </box>
            </Show>
          </box>
        </Show>

        <Show when={activeTab() === "mcl"}>
          <MclReplay urnFilter={mclUrnFilter()} aspectFilter={mclAspectFilter()} />
        </Show>

        <Show when={activeTab() === "replay"}>
          <EventReplay typeFilter={replayTypeFilter()} />
        </Show>

        <Show when={activeTab() === "connectors"}>
          <Show
            when={connectorDetailView() && connectors()[selectedConnectorIndex()]}
            fallback={
              <ConnectorList
                connectors={connectors()}
                selectedIndex={selectedConnectorIndex()}
                loading={connectorsLoading()}
              />
            }
          >
            <ConnectorDetail
              connectorName={connectors()[selectedConnectorIndex()]!.name}
              capabilities={connectorCapabilities()}
              loading={capabilitiesLoading()}
            />
          </Show>
        </Show>

        <Show when={activeTab() === "subscriptions"}>
          <SubscriptionList
            subscriptions={subscriptions()}
            selectedIndex={selectedSubscriptionIndex()}
            loading={subscriptionsLoading()}
          />
        </Show>

        <Show when={activeTab() === "locks"}>
          <LockList
            locks={locks()}
            selectedIndex={selectedLockIndex()}
            loading={locksLoading()}
          />
        </Show>

        <Show when={activeTab() === "secrets"}>
          <SecretsAudit
            entries={secretAuditEntries()}
            loading={secretsLoading()}
            filter={secretsFilter()}
          />
        </Show>

        <Show when={activeTab() === "operations"}>
          <OperationsTab
            operations={operations()}
            selectedIndex={selectedOperationIndex()}
            loading={operationsLoading()}
          />
        </Show>

        <Show when={activeTab() === "audit"}>
          <AuditTrail
            transactions={auditTransactions()}
            loading={auditLoading()}
            hasMore={auditHasMore()}
            selectedIndex={selectedAuditIndex()}
          />
        </Show>
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <Show
          when={!copied}
          fallback={<text foregroundColor={statusColor.healthy}>Copied!</text>}
        >
          <text>{getEventsHelpText(filterMode(), activeTab(), connectorDetailView())}</text>
        </Show>
      </box>
    </box>
  );
}
