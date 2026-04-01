/**
 * Keybinding builders and help-text helpers for the events panel.
 *
 * Extracted from events-panel.tsx to separate keybinding logic
 * from component rendering (Decision 6A).
 *
 * @see Issue #3591 — split oversized TUI modules
 */

import { jumpToStart, jumpToEnd } from "../../shared/hooks/use-list-navigation.js";
import { subTabCycleBindings } from "../../shared/components/sub-tab-bar-utils.js";
import type { TabDef } from "../../shared/hooks/use-visible-tabs.js";
import type { EventsPanelTab } from "../../stores/infra-store.js";
import type { FetchClient } from "@nexus/api-client";

// =============================================================================
// Types
// =============================================================================

export type FilterMode = "none" | "type" | "search" | "mcl_urn" | "mcl_aspect" | "acquire_path" | "secrets_filter" | "replay_filter";

export interface EventsBindingContext {
  // Active tab & UI
  readonly activeTab: EventsPanelTab;
  readonly visibleTabs: readonly TabDef<EventsPanelTab>[];
  readonly setActiveTab: (tab: EventsPanelTab) => void;

  // Filter state
  readonly filterMode: FilterMode;
  readonly filterBuffer: string;
  readonly setFilterMode: (mode: FilterMode) => void;
  readonly setFilterBuffer: (v: string | ((prev: string) => string)) => void;

  // Events tab
  readonly events: readonly { id?: string; event: string; data: string }[];
  readonly selectedEventIndex: number;
  readonly setSelectedEventIndex: (v: number | ((prev: number) => number)) => void;
  readonly expandedEventIndex: number | null;
  readonly setExpandedEventIndex: (v: number | null | ((prev: number | null) => number | null)) => void;
  readonly filters: { eventType: string | null; search: string | null };
  readonly setFilter: (f: { eventType?: string | null; search?: string | null }) => void;
  readonly clearEvents: () => void;
  readonly copy: (text: string) => void;

  // SSE reconnect
  readonly config: { apiKey: string; baseUrl: string; agentId?: string; subject?: string; zoneId?: string };
  readonly disconnect: () => void;
  readonly connect: (baseUrl: string, apiKey: string, opts: { agentId?: string; subject?: string; zoneId?: string }) => void;

  // MCL tab
  readonly mclUrnFilter: string;
  readonly setMclUrnFilter: (v: string) => void;
  readonly mclAspectFilter: string;
  readonly setMclAspectFilter: (v: string) => void;
  readonly clearReplay: () => void;
  readonly fetchReplay: (client: FetchClient, offset: number, limit: number) => Promise<void>;

  // Replay tab
  readonly replayTypeFilter: string;
  readonly setReplayTypeFilter: (v: string) => void;
  readonly clearEventReplay: () => void;
  readonly fetchEventReplay: (opts: { event_types?: string }, client: FetchClient) => Promise<void>;

  // Connectors tab
  readonly connectors: readonly { name: string }[];
  readonly selectedConnectorIndex: number;
  readonly setSelectedConnectorIndex: (i: number) => void;
  readonly connectorDetailView: boolean;
  readonly setConnectorDetailView: (v: boolean) => void;
  readonly fetchConnectors: (client: FetchClient) => void;
  readonly fetchConnectorCapabilities: (name: string, client: FetchClient) => Promise<void>;

  // Subscriptions tab
  readonly subscriptions: readonly { subscription_id: string }[];
  readonly selectedSubscriptionIndex: number;
  readonly setSelectedSubscriptionIndex: (i: number) => void;
  readonly deleteSubscription: (id: string, client: FetchClient) => void;
  readonly testSubscription: (id: string, client: FetchClient) => void;
  readonly fetchSubscriptions: (client: FetchClient) => void;

  // Locks tab
  readonly locks: readonly { resource: string; lock_id: string }[];
  readonly selectedLockIndex: number;
  readonly setSelectedLockIndex: (i: number) => void;
  readonly acquireLock: (path: string, mode: "mutex" | "semaphore", ttlSeconds: number, client: FetchClient) => Promise<void>;
  readonly releaseLock: (resource: string, lockId: string, client: FetchClient) => void;
  readonly extendLock: (resource: string, lockId: string, ttl: number, client: FetchClient) => void;
  readonly fetchLocks: (client: FetchClient) => void;

  // Secrets tab
  readonly secretsFilter: string;
  readonly setSecretsFilter: (v: string) => void;
  readonly fetchSecretAudit: (client: FetchClient) => void;

  // Operations tab
  readonly operations: readonly unknown[];
  readonly selectedOperationIndex: number;
  readonly setSelectedOperationIndex: (i: number) => void;
  readonly fetchOperations: (client: FetchClient) => void;

  // Audit tab
  readonly auditTransactions: readonly unknown[];
  readonly selectedAuditIndex: number;
  readonly setSelectedAuditIndex: (i: number) => void;
  readonly auditHasMore: boolean;
  readonly auditNextCursor: string | null;
  readonly fetchAuditTransactions: (opts: { cursor?: string }, client: FetchClient) => Promise<void>;

  // API client & confirmation
  readonly apiClient: FetchClient | null;
  readonly confirm: (title: string, message: string) => Promise<boolean>;
}

// =============================================================================
// Tab-aware dispatchers
// =============================================================================

export function currentItemCount(ctx: EventsBindingContext): number {
  switch (ctx.activeTab) {
    case "events": return ctx.events.length;
    case "connectors": return ctx.connectors.length;
    case "subscriptions": return ctx.subscriptions.length;
    case "locks": return ctx.locks.length;
    case "operations": return (ctx.operations as readonly unknown[]).length;
    case "audit": return (ctx.auditTransactions as readonly unknown[]).length;
    default: return 0;
  }
}

export function currentSelectedIndex(ctx: EventsBindingContext): number {
  switch (ctx.activeTab) {
    case "connectors": return ctx.selectedConnectorIndex;
    case "subscriptions": return ctx.selectedSubscriptionIndex;
    case "locks": return ctx.selectedLockIndex;
    case "operations": return ctx.selectedOperationIndex;
    case "audit": return ctx.selectedAuditIndex;
    default: return 0;
  }
}

export function setCurrentSelectedIndex(ctx: EventsBindingContext, index: number): void {
  switch (ctx.activeTab) {
    case "events": ctx.setSelectedEventIndex(index); break;
    case "connectors": ctx.setSelectedConnectorIndex(index); break;
    case "subscriptions": ctx.setSelectedSubscriptionIndex(index); break;
    case "locks": ctx.setSelectedLockIndex(index); break;
    case "operations": ctx.setSelectedOperationIndex(index); break;
    case "audit": ctx.setSelectedAuditIndex(index); break;
  }
}

export function refresh(ctx: EventsBindingContext): void {
  if (ctx.activeTab === "events") {
    if (ctx.config.apiKey && ctx.config.baseUrl) {
      ctx.disconnect();
      ctx.connect(ctx.config.baseUrl, ctx.config.apiKey, {
        agentId: ctx.config.agentId,
        subject: ctx.config.subject,
        zoneId: ctx.config.zoneId,
      });
    }
  } else if (ctx.activeTab === "mcl" && ctx.apiClient) {
    ctx.clearReplay();
    void ctx.fetchReplay(ctx.apiClient, 0, 50);
  } else if (ctx.activeTab === "replay" && ctx.apiClient) {
    ctx.clearEventReplay();
    void ctx.fetchEventReplay({ event_types: ctx.replayTypeFilter || undefined }, ctx.apiClient);
  } else if (ctx.apiClient) {
    if (ctx.activeTab === "connectors") { ctx.fetchConnectors(ctx.apiClient); ctx.setConnectorDetailView(false); }
    else if (ctx.activeTab === "subscriptions") ctx.fetchSubscriptions(ctx.apiClient);
    else if (ctx.activeTab === "locks") ctx.fetchLocks(ctx.apiClient);
    else if (ctx.activeTab === "secrets") ctx.fetchSecretAudit(ctx.apiClient);
    else if (ctx.activeTab === "operations") ctx.fetchOperations(ctx.apiClient);
    else if (ctx.activeTab === "audit") void ctx.fetchAuditTransactions({}, ctx.apiClient);
  }
}

// =============================================================================
// Keybinding builders (Decision 6A)
// =============================================================================

/** Filter input mode bindings. */
function getFilterModeBindings(ctx: EventsBindingContext): Record<string, () => void> {
  return {
    return: () => {
      const value = ctx.filterBuffer.trim() || "";
      if (ctx.filterMode === "type") {
        ctx.setFilter({ eventType: value || null });
      } else if (ctx.filterMode === "search") {
        ctx.setFilter({ search: value || null });
      } else if (ctx.filterMode === "mcl_urn") {
        ctx.setMclUrnFilter(value);
      } else if (ctx.filterMode === "mcl_aspect") {
        ctx.setMclAspectFilter(value);
      } else if (ctx.filterMode === "acquire_path") {
        if (value && ctx.apiClient) {
          ctx.acquireLock(value, "mutex", 60, ctx.apiClient);
        }
      } else if (ctx.filterMode === "secrets_filter") {
        ctx.setSecretsFilter(value);
      } else if (ctx.filterMode === "replay_filter") {
        ctx.setReplayTypeFilter(value);
        if (ctx.apiClient) void ctx.fetchEventReplay({ event_types: value || undefined }, ctx.apiClient);
      }
      ctx.setFilterMode("none");
      ctx.setFilterBuffer("");
    },
    escape: () => {
      ctx.setFilterMode("none");
      ctx.setFilterBuffer("");
    },
    backspace: () => {
      ctx.setFilterBuffer((b) => b.slice(0, -1));
    },
  };
}

/** Normal mode bindings. */
function getNormalModeBindings(ctx: EventsBindingContext): Record<string, () => void> {
  return {
    j: () => {
      if (ctx.activeTab === "events") {
        ctx.setSelectedEventIndex((i) => Math.min(i + 1, ctx.events.length - 1));
      } else {
        const max = currentItemCount(ctx) - 1;
        if (max >= 0) setCurrentSelectedIndex(ctx, Math.min(currentSelectedIndex(ctx) + 1, max));
      }
    },
    down: () => {
      if (ctx.activeTab === "events") {
        ctx.setSelectedEventIndex((i) => Math.min(i + 1, ctx.events.length - 1));
      } else {
        const max = currentItemCount(ctx) - 1;
        if (max >= 0) setCurrentSelectedIndex(ctx, Math.min(currentSelectedIndex(ctx) + 1, max));
      }
    },
    k: () => {
      if (ctx.activeTab === "events") {
        ctx.setSelectedEventIndex((i) => Math.max(i - 1, 0));
      } else {
        setCurrentSelectedIndex(ctx, Math.max(currentSelectedIndex(ctx) - 1, 0));
      }
    },
    up: () => {
      if (ctx.activeTab === "events") {
        ctx.setSelectedEventIndex((i) => Math.max(i - 1, 0));
      } else {
        setCurrentSelectedIndex(ctx, Math.max(currentSelectedIndex(ctx) - 1, 0));
      }
    },
    return: () => {
      if (ctx.activeTab === "events" && ctx.selectedEventIndex >= 0 && ctx.selectedEventIndex < ctx.events.length) {
        ctx.setExpandedEventIndex((prev) => prev === ctx.selectedEventIndex ? null : ctx.selectedEventIndex);
      } else if (ctx.activeTab === "connectors" && ctx.apiClient) {
        const conn = ctx.connectors[ctx.selectedConnectorIndex];
        if (conn) {
          void ctx.fetchConnectorCapabilities(conn.name, ctx.apiClient);
          ctx.setConnectorDetailView(true);
        }
      }
    },
    escape: () => {
      if (ctx.activeTab === "events" && ctx.expandedEventIndex !== null) {
        ctx.setExpandedEventIndex(null);
      } else if (ctx.activeTab === "connectors" && ctx.connectorDetailView) {
        ctx.setConnectorDetailView(false);
      }
    },
    ...subTabCycleBindings(ctx.visibleTabs, ctx.activeTab, ctx.setActiveTab),
    c: () => ctx.clearEvents(),
    r: () => refresh(ctx),
    f: () => {
      if (ctx.activeTab === "events") {
        ctx.setFilterMode("type");
        ctx.setFilterBuffer(ctx.filters.eventType ?? "");
      } else if (ctx.activeTab === "replay") {
        ctx.setFilterMode("replay_filter");
        ctx.setFilterBuffer(ctx.replayTypeFilter);
      }
    },
    m: () => {
      if (ctx.activeTab === "audit" && ctx.auditHasMore && ctx.auditNextCursor && ctx.apiClient) {
        void ctx.fetchAuditTransactions({ cursor: ctx.auditNextCursor }, ctx.apiClient);
      }
    },
    s: () => {
      if (ctx.activeTab === "events") {
        ctx.setFilterMode("search");
        ctx.setFilterBuffer(ctx.filters.search ?? "");
      }
    },
    u: () => {
      if (ctx.activeTab === "mcl") {
        ctx.setFilterMode("mcl_urn");
        ctx.setFilterBuffer(ctx.mclUrnFilter);
      }
    },
    n: () => {
      if (ctx.activeTab === "mcl") {
        ctx.setFilterMode("mcl_aspect");
        ctx.setFilterBuffer(ctx.mclAspectFilter);
      } else if (ctx.activeTab === "locks") {
        ctx.setFilterMode("acquire_path");
        ctx.setFilterBuffer("");
      }
    },
    d: async () => {
      if (ctx.activeTab === "subscriptions" && ctx.apiClient) {
        const sub = ctx.subscriptions[ctx.selectedSubscriptionIndex];
        if (sub) {
          const ok = await ctx.confirm("Delete subscription?", "Delete this event subscription.");
          if (!ok) return;
          ctx.deleteSubscription(sub.subscription_id, ctx.apiClient);
        }
      } else if (ctx.activeTab === "locks" && ctx.apiClient) {
        const lock = ctx.locks[ctx.selectedLockIndex];
        if (lock) {
          const ok = await ctx.confirm("Release lock?", "Release this lock. Other waiters may acquire it.");
          if (!ok) return;
          ctx.releaseLock(lock.resource, lock.lock_id, ctx.apiClient);
        }
      }
    },
    t: () => {
      if (ctx.activeTab === "subscriptions" && ctx.apiClient) {
        const sub = ctx.subscriptions[ctx.selectedSubscriptionIndex];
        if (sub) ctx.testSubscription(sub.subscription_id, ctx.apiClient);
      }
    },
    e: () => {
      if (ctx.activeTab === "locks" && ctx.apiClient) {
        const lock = ctx.locks[ctx.selectedLockIndex];
        if (lock) ctx.extendLock(lock.resource, lock.lock_id, 60, ctx.apiClient);
      }
    },
    "/": () => {
      if (ctx.activeTab === "secrets") {
        ctx.setFilterMode("secrets_filter");
        ctx.setFilterBuffer(ctx.secretsFilter);
      }
    },
    y: () => {
      if (ctx.activeTab === "events") {
        const idx = ctx.selectedEventIndex >= 0 ? ctx.selectedEventIndex : ctx.events.length - 1;
        const event = ctx.events[idx];
        if (event) ctx.copy(event.data);
      }
    },
    g: () => {
      setCurrentSelectedIndex(ctx, jumpToStart());
    },
    "shift+g": () => {
      setCurrentSelectedIndex(ctx, jumpToEnd(currentItemCount(ctx)));
    },
  };
}

/** Top-level binding dispatch based on current mode. */
export function getEventsKeyBindings(
  overlayActive: boolean,
  ctx: EventsBindingContext,
): Record<string, () => void> {
  if (overlayActive) return {};
  if (ctx.filterMode !== "none") return getFilterModeBindings(ctx);
  return getNormalModeBindings(ctx);
}

/** Handle unhandled keys in filter input mode. */
export function handleEventsUnhandledKey(
  filterMode: FilterMode,
  setFilterBuffer: (v: string | ((prev: string) => string)) => void,
  keyName: string,
): void {
  if (filterMode === "none") return;
  if (keyName.length === 1) {
    setFilterBuffer((b) => b + keyName);
  } else if (keyName === "space") {
    setFilterBuffer((b) => b + " ");
  }
}

// =============================================================================
// Help text
// =============================================================================

export function getEventsHelpText(
  filterMode: FilterMode,
  activeTab: EventsPanelTab,
  connectorDetailView: boolean,
): string {
  if (filterMode !== "none") return "Type value, Enter:apply, Escape:cancel, Backspace:delete";

  switch (activeTab) {
    case "events":
      return "j/k:navigate  Enter:expand  f:filter type  s:search  c:clear  r:reconnect  y:copy  Tab:switch";
    case "mcl":
      return "u:filter URN  n:filter aspect  r:refresh  Tab:switch tab";
    case "replay":
      return "f:filter event type  r:refresh  Tab:switch tab";
    case "connectors":
      return connectorDetailView
        ? "Escape:back  r:refresh  Tab:switch tab"
        : "j/k:navigate  Enter:capabilities  r:refresh  Tab:switch tab";
    case "subscriptions":
      return "j/k:navigate  d:delete  t:test  r:refresh  Tab:switch tab";
    case "locks":
      return "j/k:navigate  n:acquire  d:release  e:extend  r:refresh  Tab:switch tab";
    case "secrets":
      return "/:filter  r:refresh  Tab:switch tab";
    case "audit":
      return "j/k:navigate  m:load more  r:refresh  Tab:switch tab";
    default:
      return "j/k:navigate  r:refresh  Tab:switch tab";
  }
}

// =============================================================================
// Utilities
// =============================================================================

export function formatEventData(data: string): string {
  try {
    const parsed = JSON.parse(data);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return data;
  }
}
