import type { TabDef } from "./hooks/use-visible-tabs.js";
import type { PanelId } from "../stores/global-store.js";
import type { AccessTab } from "../stores/access-store.js";
import type { AgentTab } from "../stores/agents-store.js";
import type { SearchTab } from "../stores/search-store.js";
import type { ZoneTab } from "../stores/zones-store.js";
import type { WorkflowTab } from "../stores/workflows-store.js";
import type { PaymentsTab } from "../stores/payments-store.js";
import type { EventsPanelTab } from "../stores/infra-store.js";
import { NAV_ITEMS, type NavItem } from "./nav-items.js";

export interface TopLevelTab {
  readonly id: PanelId;
  readonly label: string;
  readonly shortcut: string;
}

export interface PanelDescriptor {
  readonly id: PanelId;
  readonly tabLabel: string;
  readonly breadcrumbLabel: string;
  readonly shortcut: string;
}

export const PANEL_DESCRIPTORS: Readonly<Record<PanelId, PanelDescriptor>> = {
  files:          { id: "files",          tabLabel: "Files",  breadcrumbLabel: "Files",      shortcut: "1" },
  versions:       { id: "versions",       tabLabel: "Ver",    breadcrumbLabel: "Versions",   shortcut: "2" },
  agents:         { id: "agents",         tabLabel: "Agent",  breadcrumbLabel: "Agents",     shortcut: "3" },
  zones:          { id: "zones",          tabLabel: "Zone",   breadcrumbLabel: "Zones",      shortcut: "4" },
  access:         { id: "access",         tabLabel: "ACL",    breadcrumbLabel: "Access",     shortcut: "5" },
  payments:       { id: "payments",       tabLabel: "Pay",    breadcrumbLabel: "Payments",   shortcut: "6" },
  search:         { id: "search",         tabLabel: "Find",   breadcrumbLabel: "Search",     shortcut: "7" },
  workflows:      { id: "workflows",      tabLabel: "Flow",   breadcrumbLabel: "Workflows",  shortcut: "8" },
  infrastructure: { id: "infrastructure", tabLabel: "Event",  breadcrumbLabel: "Events",     shortcut: "9" },
  console:        { id: "console",        tabLabel: "CLI",    breadcrumbLabel: "Console",    shortcut: "0" },
  connectors:     { id: "connectors",     tabLabel: "Conn",   breadcrumbLabel: "Connectors", shortcut: "·" },
  stack:          { id: "stack",          tabLabel: "Stack",  breadcrumbLabel: "Stack",      shortcut: "○" },
};

export const PANEL_TABS: readonly TopLevelTab[] = NAV_ITEMS.map(({ id, label, shortcut }) => ({
  id,
  label,
  shortcut,
}));

export const ACCESS_TABS: readonly TabDef<AccessTab>[] = [
  { id: "manifests", label: "Manifests", brick: "access_manifest" },
  { id: "alerts", label: "Alerts", brick: "governance" },
  { id: "credentials", label: "Credentials", brick: "auth" },
  { id: "fraud", label: "Fraud", brick: "governance" },
  { id: "delegations", label: "Delegations", brick: "delegation" },
];

export const AGENT_TABS: readonly TabDef<AgentTab>[] = [
  { id: "status", label: "Status", brick: "agent_runtime" },
  { id: "delegations", label: "Delegations", brick: "delegation" },
  { id: "inbox", label: "Inbox", brick: "ipc" },
  { id: "trajectories", label: "Trajectories", brick: "agent_runtime" },
];

export const SEARCH_TABS: readonly TabDef<SearchTab>[] = [
  { id: "search", label: "Search", brick: "search" },
  { id: "knowledge", label: "Knowledge", brick: "catalog" },
  { id: "memories", label: "Memories", brick: "memory" },
  { id: "playbooks", label: "Playbooks", brick: null },
  { id: "ask", label: "Ask", brick: "rlm" },
  { id: "columns", label: "Columns", brick: "catalog" },
];

export const ZONE_TABS: readonly TabDef<ZoneTab>[] = [
  { id: "zones", label: "Zones", brick: null },
  { id: "bricks", label: "Bricks", brick: null },
  { id: "drift", label: "Drift", brick: null },
  { id: "reindex", label: "Reindex", brick: ["search", "versioning"] },
  { id: "workspaces", label: "Workspaces", brick: "workspace" },
  { id: "mcp", label: "MCP", brick: "mcp" },
  { id: "cache", label: "Cache", brick: "cache" },
];

export const WORKFLOW_TABS: readonly TabDef<WorkflowTab>[] = [
  { id: "workflows", label: "Workflows", brick: null },
  { id: "executions", label: "Executions", brick: null },
  { id: "scheduler", label: "Scheduler", brick: null },
];

export const PAYMENTS_TABS: readonly TabDef<PaymentsTab>[] = [
  { id: "balance", label: "Balance", brick: null },
  { id: "reservations", label: "Reservations", brick: null },
  { id: "transactions", label: "Transactions", brick: null },
  { id: "policies", label: "Policies", brick: null },
  { id: "approvals", label: "Approvals", brick: null },
];

export const EVENTS_TABS: readonly TabDef<EventsPanelTab>[] = [
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

export function getTabLabel<T extends string>(
  tabs: readonly { readonly id: T; readonly label: string }[],
  activeTab: T | null | undefined,
): string | null {
  if (!activeTab) return null;
  return tabs.find((tab) => tab.id === activeTab)?.label ?? null;
}
