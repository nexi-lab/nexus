/**
 * Keyboard bindings, help text, and formatting utilities for the
 * Events/Infrastructure panel.
 *
 * Extracted from events-panel.tsx so the complex keyboard logic is
 * testable in isolation without requiring the full React render tree.
 *
 * @see Issue #3623
 */

import type { Setter } from "solid-js";
import type { SseEvent } from "@nexus-ai-fs/api-client";
import type { FetchClient, NexusClientOptions } from "@nexus-ai-fs/api-client";
import { subTabCycleBindings } from "../../shared/components/sub-tab-bar-utils.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import type { TabDef } from "../../shared/hooks/use-visible-tabs.js";
import type {
  EventsPanelTab,
  Connector,
  Subscription,
  Lock,
  OperationItem,
  AuditTransaction,
} from "../../stores/infra-store.js";
import type { EventFilters, SseIdentity } from "../../stores/events-store.js";

// =============================================================================
// Types
// =============================================================================

/**
 * Active filter input mode for the events panel.
 * "none" means no input is active; all other values gate character input
 * to the filter buffer via handleEventsUnhandledKey.
 */
export type FilterMode =
  | "none"
  | "type"
  | "search"
  | "mcl_urn"
  | "mcl_aspect"
  | "acquire_path"
  | "replay_filter"
  | "secrets_filter";

/**
 * All state and callbacks needed to build keyboard bindings for the
 * Events/Infrastructure panel. Passed from the panel component to
 * getEventsKeyBindings so the keybinding logic stays pure and testable.
 */
export interface EventsBindingContext {
  // Tab navigation
  readonly activeTab: EventsPanelTab;
  readonly visibleTabs: readonly TabDef<EventsPanelTab>[];
  readonly setActiveTab: (tab: EventsPanelTab) => void;

  // Filter state
  readonly filterMode: FilterMode;
  readonly filterBuffer: string;
  readonly setFilterMode: Setter<FilterMode>;
  readonly setFilterBuffer: Setter<string>;

  // Events (SSE)
  readonly events: readonly SseEvent[];
  readonly selectedEventIndex: number;
  readonly setSelectedEventIndex: Setter<number>;
  readonly expandedEventIndex: number | null;
  readonly setExpandedEventIndex: Setter<number | null>;
  readonly filters: EventFilters;
  readonly setFilter: (filters: Partial<EventFilters>) => void;
  readonly clearEvents: () => void;
  readonly copy: (text: string) => void;

  // SSE connection
  readonly config: NexusClientOptions;
  readonly connect: (baseUrl: string, apiKey: string, identity?: SseIdentity) => void;
  readonly disconnect: () => void;

  // MCL
  readonly mclUrnFilter: string;
  readonly setMclUrnFilter: Setter<string>;
  readonly mclAspectFilter: string;
  readonly setMclAspectFilter: Setter<string>;
  readonly clearReplay: () => void;
  readonly fetchReplay: (client: FetchClient, offset: number, limit: number) => Promise<void>;

  // Replay
  readonly replayTypeFilter: string;
  readonly setReplayTypeFilter: Setter<string>;
  readonly clearEventReplay: () => void;
  readonly fetchEventReplay: (params: Record<string, unknown>, client: FetchClient) => Promise<void>;

  // Connectors
  readonly connectors: readonly Connector[];
  readonly selectedConnectorIndex: number;
  readonly setSelectedConnectorIndex: (index: number) => void;
  readonly connectorDetailView: boolean;
  readonly setConnectorDetailView: Setter<boolean>;
  readonly fetchConnectors: (client: FetchClient) => Promise<void>;
  readonly fetchConnectorCapabilities: (connectorName: string, client: FetchClient) => Promise<void>;

  // Subscriptions
  readonly subscriptions: readonly Subscription[];
  readonly selectedSubscriptionIndex: number;
  readonly setSelectedSubscriptionIndex: (index: number) => void;
  readonly deleteSubscription: (id: string, client: FetchClient) => Promise<void>;
  readonly testSubscription: (id: string, client: FetchClient) => Promise<void>;
  readonly fetchSubscriptions: (client: FetchClient) => Promise<void>;

  // Locks
  readonly locks: readonly Lock[];
  readonly selectedLockIndex: number;
  readonly setSelectedLockIndex: (index: number) => void;
  readonly acquireLock: (path: string, mode: "mutex" | "semaphore", ttlSeconds: number, client: FetchClient) => Promise<void>;
  readonly releaseLock: (path: string, lockId: string, client: FetchClient) => Promise<void>;
  readonly extendLock: (path: string, lockId: string, ttlSeconds: number, client: FetchClient) => Promise<void>;
  readonly fetchLocks: (client: FetchClient) => Promise<void>;

  // Secrets
  readonly secretsFilter: string;
  readonly setSecretsFilter: Setter<string>;
  readonly fetchSecretAudit: (client: FetchClient) => Promise<void>;

  // Operations
  readonly operations: readonly OperationItem[];
  readonly selectedOperationIndex: number;
  readonly setSelectedOperationIndex: (index: number) => void;
  readonly fetchOperations: (client: FetchClient) => Promise<void>;

  // Audit
  readonly auditTransactions: readonly AuditTransaction[];
  readonly selectedAuditIndex: number;
  readonly setSelectedAuditIndex: Setter<number>;
  readonly auditHasMore: boolean;
  readonly auditNextCursor: string | null;
  readonly fetchAuditTransactions: (filters: { cursor?: string; limit?: number }, client: FetchClient) => Promise<void>;

  // API client + confirm
  readonly apiClient: FetchClient | null;
  readonly confirm: (title: string, message: string) => Promise<boolean>;
}

// =============================================================================
// Help text
// =============================================================================

const FILTER_MODE_HELP = "Type to enter filter  Enter:apply  Backspace:delete  Escape:cancel";

const TAB_HELP: Readonly<Record<EventsPanelTab, string>> = {
  events:        "f:type filter  s:search  j/k:scroll  Enter:expand  y:copy  c:clear  r:reconnect  Tab:tab  q:quit",
  mcl:           "f:URN filter  s:aspect filter  Tab:tab  r:refresh  q:quit",
  replay:        "f:type filter  Tab:tab  r:refresh  q:quit",
  connectors:    "j/k:navigate  Enter:detail  Tab:tab  r:refresh  q:quit",
  subscriptions: "j/k:navigate  d:delete  t:test  Tab:tab  r:refresh  q:quit",
  locks:         "j/k:navigate  a:acquire  x:release  e:extend  Tab:tab  r:refresh  q:quit",
  secrets:       "j/k:navigate  f:filter  Tab:tab  r:refresh  q:quit",
  operations:    "j/k:navigate  Tab:tab  r:refresh  q:quit",
  audit:         "j/k:navigate  ]:more  Tab:tab  r:refresh  q:quit",
};

export function getEventsHelpText(
  filterMode: FilterMode,
  activeTab: EventsPanelTab,
  connectorDetailView: boolean,
): string {
  if (filterMode !== "none") return FILTER_MODE_HELP;
  if (activeTab === "connectors" && connectorDetailView) {
    return "Escape:back  Tab:tab  q:quit";
  }
  return TAB_HELP[activeTab] ?? "";
}

// =============================================================================
// Filter mode bindings
// =============================================================================

function getFilterModeBindings(ctx: EventsBindingContext): Record<string, () => void> {
  const {
    filterMode, filterBuffer, setFilterMode, setFilterBuffer,
    setFilter, setMclUrnFilter, setMclAspectFilter,
    setReplayTypeFilter, setSecretsFilter,
    acquireLock, apiClient,
  } = ctx;

  const cancelFilter = (): void => {
    setFilterMode("none");
    setFilterBuffer("");
  };

  const applyFilter = (): void => {
    const val = filterBuffer.trim();
    if (filterMode === "type") {
      setFilter({ eventType: val || null });
    } else if (filterMode === "search") {
      setFilter({ search: val || null });
    } else if (filterMode === "mcl_urn") {
      setMclUrnFilter(val);
    } else if (filterMode === "mcl_aspect") {
      setMclAspectFilter(val);
    } else if (filterMode === "replay_filter") {
      setReplayTypeFilter(val);
    } else if (filterMode === "secrets_filter") {
      setSecretsFilter(val);
    } else if (filterMode === "acquire_path") {
      if (val && apiClient) {
        void acquireLock(val, "mutex", 30, apiClient);
      }
    }
    setFilterMode("none");
    setFilterBuffer("");
  };

  return {
    escape: cancelFilter,
    return: applyFilter,
    backspace: () => setFilterBuffer((b) => b.slice(0, -1)),
  };
}

// =============================================================================
// Normal mode bindings
// =============================================================================

function getNormalModeBindings(ctx: EventsBindingContext): Record<string, () => void> {
  const {
    activeTab, visibleTabs, setActiveTab,
    setFilterMode, setFilterBuffer,
    events, selectedEventIndex, setSelectedEventIndex,
    expandedEventIndex, setExpandedEventIndex,
    filters, setFilter, clearEvents, copy,
    config, connect, disconnect,
    mclUrnFilter, mclAspectFilter,
    fetchReplay,
    replayTypeFilter, fetchEventReplay,
    connectors, selectedConnectorIndex, setSelectedConnectorIndex,
    connectorDetailView, setConnectorDetailView,
    fetchConnectors, fetchConnectorCapabilities,
    subscriptions, selectedSubscriptionIndex, setSelectedSubscriptionIndex,
    deleteSubscription, testSubscription, fetchSubscriptions,
    locks, selectedLockIndex, setSelectedLockIndex,
    releaseLock, extendLock, fetchLocks,
    secretsFilter, fetchSecretAudit,
    operations, selectedOperationIndex, setSelectedOperationIndex, fetchOperations,
    auditTransactions, selectedAuditIndex, setSelectedAuditIndex,
    auditHasMore, auditNextCursor, fetchAuditTransactions,
    apiClient,
  } = ctx;

  const getIndex = (): number => {
    if (activeTab === "events") return selectedEventIndex >= 0 ? selectedEventIndex : events.length - 1;
    if (activeTab === "connectors") return selectedConnectorIndex;
    if (activeTab === "subscriptions") return selectedSubscriptionIndex;
    if (activeTab === "locks") return selectedLockIndex;
    if (activeTab === "operations") return selectedOperationIndex;
    if (activeTab === "audit") return selectedAuditIndex;
    return 0;
  };

  const setIndex = (i: number): void => {
    if (activeTab === "events") setSelectedEventIndex(i);
    else if (activeTab === "connectors") setSelectedConnectorIndex(i);
    else if (activeTab === "subscriptions") setSelectedSubscriptionIndex(i);
    else if (activeTab === "locks") setSelectedLockIndex(i);
    else if (activeTab === "operations") setSelectedOperationIndex(i);
    else if (activeTab === "audit") setSelectedAuditIndex(i);
  };

  const getLength = (): number => {
    if (activeTab === "events") return events.length;
    if (activeTab === "connectors") return connectors.length;
    if (activeTab === "subscriptions") return subscriptions.length;
    if (activeTab === "locks") return locks.length;
    if (activeTab === "operations") return operations.length;
    if (activeTab === "audit") return auditTransactions.length;
    return 0;
  };

  const refreshCurrentTab = (): void => {
    if (!apiClient) return;
    if (activeTab === "events") {
      if (config.apiKey && config.baseUrl) {
        disconnect();
        connect(config.baseUrl, config.apiKey, {
          agentId: config.agentId,
          subject: config.subject,
          zoneId: config.zoneId,
        });
      }
    } else if (activeTab === "mcl") {
      void fetchReplay(apiClient, 0, 50);
    } else if (activeTab === "replay") {
      void fetchEventReplay({}, apiClient);
    } else if (activeTab === "connectors") {
      void fetchConnectors(apiClient);
    } else if (activeTab === "subscriptions") {
      void fetchSubscriptions(apiClient);
    } else if (activeTab === "locks") {
      void fetchLocks(apiClient);
    } else if (activeTab === "secrets") {
      void fetchSecretAudit(apiClient);
    } else if (activeTab === "operations") {
      void fetchOperations(apiClient);
    } else if (activeTab === "audit") {
      void fetchAuditTransactions({}, apiClient);
    }
  };

  return {
    ...listNavigationBindings({ getIndex, setIndex, getLength }),
    ...subTabCycleBindings(visibleTabs, activeTab, setActiveTab),

    escape: () => {
      if (expandedEventIndex !== null) {
        setExpandedEventIndex(null);
      } else if (connectorDetailView) {
        setConnectorDetailView(false);
      }
    },

    return: () => {
      if (activeTab === "events") {
        const idx = selectedEventIndex >= 0 ? selectedEventIndex : events.length - 1;
        if (expandedEventIndex === idx) {
          setExpandedEventIndex(null);
        } else if (events[idx]) {
          setExpandedEventIndex(idx);
        }
      } else if (activeTab === "connectors" && !connectorDetailView) {
        const connector = connectors[selectedConnectorIndex];
        if (connector && apiClient) {
          setConnectorDetailView(true);
          void fetchConnectorCapabilities(connector.name, apiClient);
        }
      }
    },

    r: refreshCurrentTab,

    c: () => {
      if (activeTab === "events") clearEvents();
    },

    y: () => {
      if (activeTab === "events") {
        const idx = selectedEventIndex >= 0 ? selectedEventIndex : events.length - 1;
        const event = events[idx];
        if (event?.id) copy(event.id);
        else if (event?.data) copy(event.data);
      }
    },

    f: () => {
      if (activeTab === "events") {
        setFilterMode("type");
        setFilterBuffer(filters.eventType ?? "");
      } else if (activeTab === "mcl") {
        setFilterMode("mcl_urn");
        setFilterBuffer(mclUrnFilter);
      } else if (activeTab === "replay") {
        setFilterMode("replay_filter");
        setFilterBuffer(replayTypeFilter);
      } else if (activeTab === "secrets") {
        setFilterMode("secrets_filter");
        setFilterBuffer(secretsFilter);
      }
    },

    s: () => {
      if (activeTab === "events") {
        setFilterMode("search");
        setFilterBuffer(filters.search ?? "");
      } else if (activeTab === "mcl") {
        setFilterMode("mcl_aspect");
        setFilterBuffer(mclAspectFilter);
      }
    },

    d: () => {
      if (activeTab === "subscriptions" && apiClient) {
        const sub = subscriptions[selectedSubscriptionIndex];
        if (sub) void deleteSubscription(sub.subscription_id, apiClient);
      }
    },

    t: () => {
      if (activeTab === "subscriptions" && apiClient) {
        const sub = subscriptions[selectedSubscriptionIndex];
        if (sub) void testSubscription(sub.subscription_id, apiClient);
      }
    },

    a: () => {
      if (activeTab === "locks") {
        setFilterMode("acquire_path");
        setFilterBuffer("");
      }
    },

    x: () => {
      if (activeTab === "locks" && apiClient) {
        const lock = locks[selectedLockIndex];
        if (lock) void releaseLock(lock.resource, lock.lock_id, apiClient);
      }
    },

    e: () => {
      if (activeTab === "locks" && apiClient) {
        const lock = locks[selectedLockIndex];
        if (lock) void extendLock(lock.resource, lock.lock_id, 30, apiClient);
      }
    },

    "]": () => {
      if (activeTab === "audit" && auditHasMore && apiClient) {
        void fetchAuditTransactions({ cursor: auditNextCursor ?? undefined }, apiClient);
      }
    },
  };
}

// =============================================================================
// Public API
// =============================================================================

/**
 * Returns the keyboard binding map for the Events/Infrastructure panel.
 * Returns {} when an overlay is active (panel doesn't process keys then).
 */
export function getEventsKeyBindings(
  overlayActive: boolean,
  ctx: EventsBindingContext,
): Record<string, () => void> {
  if (overlayActive) return {};
  if (ctx.filterMode !== "none") return getFilterModeBindings(ctx);
  return getNormalModeBindings(ctx);
}

/**
 * Handles unmatched keystrokes in filter modes by appending characters
 * to the filter buffer. Passed as the second argument to useKeyboard.
 */
export function handleEventsUnhandledKey(
  filterMode: FilterMode,
  setFilterBuffer: Setter<string>,
  keyName: string,
): void {
  if (filterMode === "none") return;
  if (keyName === "space") {
    setFilterBuffer((b) => b + " ");
  } else if (keyName.length === 1) {
    setFilterBuffer((b) => b + keyName);
  }
}

/**
 * Formats raw SSE event data for the expanded detail view.
 * Attempts JSON pretty-print; falls back to the raw string.
 */
export function formatEventData(data: string): string {
  if (!data) return "";
  try {
    const parsed: unknown = JSON.parse(data);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return data;
  }
}
