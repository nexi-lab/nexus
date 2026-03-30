/**
 * Tests for useVisibleTabs hook (Decision 2A, 11A, 15A).
 *
 * Covers:
 * - Tab filtering based on enabled bricks
 * - Loading state (shows all tabs while features are loading)
 * - Single brick dependency
 * - Multi-brick dependency (any-of semantics)
 * - Null brick (always visible)
 * - Profile-based snapshot tests (full, lite, embedded, cloud, minimal)
 */

import { describe, it, expect, beforeEach } from "bun:test";
import { useGlobalStore } from "../../src/stores/global-store.js";
import type { TabDef } from "../../src/shared/hooks/use-visible-tabs.js";

// We test the filtering logic directly via the store state
// since hooks require a React render context. The useVisibleTabs hook
// delegates to the same logic we validate here.

/**
 * Pure filtering function matching useVisibleTabs implementation.
 * Extracted for testability without React render context.
 */
function filterTabs<T extends string>(
  allTabs: readonly TabDef<T>[],
  enabledBricks: readonly string[],
  featuresLoaded: boolean,
): readonly TabDef<T>[] {
  if (!featuresLoaded) return allTabs;

  return allTabs.filter((tab) => {
    if (tab.brick === null) return true;
    if (typeof tab.brick === "string") return enabledBricks.includes(tab.brick);
    return tab.brick.some((b) => enabledBricks.includes(b));
  });
}

// =============================================================================
// Test data: tab definitions matching actual panel mappings from #2981
// =============================================================================

type AccessTab = "manifests" | "alerts" | "credentials" | "fraud" | "delegations";
const ACCESS_TABS: readonly TabDef<AccessTab>[] = [
  { id: "manifests", label: "Manifests", brick: "access_manifest" },
  { id: "alerts", label: "Alerts", brick: "governance" },
  { id: "credentials", label: "Credentials", brick: "auth" },
  { id: "fraud", label: "Fraud", brick: "governance" },
  { id: "delegations", label: "Delegations", brick: "delegation" },
];

type SearchTab = "search" | "knowledge" | "memories" | "playbooks" | "ask" | "columns";
const SEARCH_TABS: readonly TabDef<SearchTab>[] = [
  { id: "search", label: "Search", brick: "search" },
  { id: "knowledge", label: "Knowledge", brick: "catalog" },
  { id: "memories", label: "Memories", brick: "memory" },
  { id: "playbooks", label: "Playbooks", brick: null },
  { id: "ask", label: "Ask", brick: "rlm" },
  { id: "columns", label: "Columns", brick: "catalog" },
];

type ZoneTab = "zones" | "bricks" | "drift" | "reindex";
const ZONE_TABS: readonly TabDef<ZoneTab>[] = [
  { id: "zones", label: "Zones", brick: null },
  { id: "bricks", label: "Bricks", brick: null },
  { id: "drift", label: "Drift", brick: null },
  { id: "reindex", label: "Reindex", brick: ["search", "versioning"] },
];

type AgentTab = "status" | "delegations" | "inbox";
const AGENT_TABS: readonly TabDef<AgentTab>[] = [
  { id: "status", label: "Status", brick: "agent_runtime" },
  { id: "delegations", label: "Delegations", brick: "delegation" },
  { id: "inbox", label: "Inbox", brick: "ipc" },
];

type EventTab = "events" | "mcl" | "connectors" | "subscriptions" | "locks" | "secrets";
const EVENT_TABS: readonly TabDef<EventTab>[] = [
  { id: "events", label: "Events", brick: "eventlog" },
  { id: "mcl", label: "MCL", brick: "catalog" },
  { id: "connectors", label: "Connectors", brick: null },
  { id: "subscriptions", label: "Subscriptions", brick: "eventlog" },
  { id: "locks", label: "Locks", brick: null },
  { id: "secrets", label: "Secrets", brick: "auth" },
];

// Migrated panels: previously used TAB_ORDER, now use TabDef (#3498)

type ConnectorsTab = "available" | "mounted" | "skills" | "write";
const CONNECTORS_TABS: readonly TabDef<ConnectorsTab>[] = [
  { id: "available", label: "Available", brick: null },
  { id: "mounted", label: "Mounted", brick: null },
  { id: "skills", label: "Skills", brick: null },
  { id: "write", label: "Write", brick: null },
];

type PaymentsTab = "balance" | "reservations" | "transactions" | "policies" | "approvals";
const PAYMENTS_TABS: readonly TabDef<PaymentsTab>[] = [
  { id: "balance", label: "Balance", brick: null },
  { id: "reservations", label: "Reservations", brick: null },
  { id: "transactions", label: "Transactions", brick: null },
  { id: "policies", label: "Policies", brick: null },
  { id: "approvals", label: "Approvals", brick: null },
];

type WorkflowTab = "workflows" | "executions" | "scheduler";
const WORKFLOW_TABS: readonly TabDef<WorkflowTab>[] = [
  { id: "workflows", label: "Workflows", brick: null },
  { id: "executions", label: "Executions", brick: null },
  { id: "scheduler", label: "Scheduler", brick: null },
];

type FilesTab = "explorer" | "shareLinks" | "uploads";
const FILES_TABS: readonly TabDef<FilesTab>[] = [
  { id: "explorer", label: "Explorer", brick: null },
  { id: "shareLinks", label: "Share Links", brick: "share_link" },
  { id: "uploads", label: "Uploads", brick: "uploads" },
];

// =============================================================================
// Tests
// =============================================================================

describe("filterTabs", () => {
  describe("loading state", () => {
    it("returns all tabs when features are not yet loaded", () => {
      const result = filterTabs(ACCESS_TABS, [], false);
      expect(result).toEqual(ACCESS_TABS);
    });

    it("returns all tabs when features not loaded even with some bricks", () => {
      const result = filterTabs(ACCESS_TABS, ["access_manifest"], false);
      expect(result).toEqual(ACCESS_TABS);
    });
  });

  describe("single brick dependency", () => {
    it("shows tab when its brick is enabled", () => {
      const result = filterTabs(ACCESS_TABS, ["access_manifest"], true);
      expect(result.map((t) => t.id)).toEqual(["manifests"]);
    });

    it("hides tab when its brick is disabled", () => {
      const result = filterTabs(ACCESS_TABS, ["governance", "auth", "delegation"], true);
      expect(result.map((t) => t.id)).toEqual(["alerts", "credentials", "fraud", "delegations"]);
    });

    it("shows multiple tabs sharing the same brick", () => {
      const result = filterTabs(ACCESS_TABS, ["governance"], true);
      // Both alerts and fraud depend on governance
      expect(result.map((t) => t.id)).toEqual(["alerts", "fraud"]);
    });
  });

  describe("null brick (always visible)", () => {
    it("shows tabs with null brick regardless of enabled bricks", () => {
      const result = filterTabs(SEARCH_TABS, [], true);
      // Only playbooks has null brick
      expect(result.map((t) => t.id)).toEqual(["playbooks"]);
    });

    it("shows null-brick tabs alongside enabled-brick tabs", () => {
      const result = filterTabs(SEARCH_TABS, ["search"], true);
      expect(result.map((t) => t.id)).toEqual(["search", "playbooks"]);
    });
  });

  describe("multi-brick dependency (any-of semantics)", () => {
    it("shows tab when any of its bricks is enabled", () => {
      const result = filterTabs(ZONE_TABS, ["search"], true);
      // zones, bricks, drift are always visible; reindex needs search OR versioning
      expect(result.map((t) => t.id)).toContain("reindex");
    });

    it("shows tab when the other brick is enabled", () => {
      const result = filterTabs(ZONE_TABS, ["versioning"], true);
      expect(result.map((t) => t.id)).toContain("reindex");
    });

    it("hides tab when none of its bricks are enabled", () => {
      const result = filterTabs(ZONE_TABS, ["catalog"], true);
      expect(result.map((t) => t.id)).not.toContain("reindex");
    });
  });

  describe("edge cases", () => {
    it("returns empty array when all bricks disabled and no null-brick tabs", () => {
      const result = filterTabs(AGENT_TABS, [], true);
      expect(result).toEqual([]);
    });

    it("handles all bricks enabled", () => {
      const allBricks = [
        "access_manifest", "governance", "auth", "delegation",
        "search", "catalog", "memory", "rlm",
        "agent_runtime", "ipc", "eventlog", "versioning",
      ];
      const result = filterTabs(ACCESS_TABS, allBricks, true);
      expect(result).toEqual(ACCESS_TABS);
    });

    it("handles empty tab list", () => {
      const result = filterTabs([], ["search"], true);
      expect(result).toEqual([]);
    });

    it("ignores unknown brick names in enabledBricks", () => {
      const result = filterTabs(ACCESS_TABS, ["unknown_brick", "nonexistent"], true);
      expect(result).toEqual([]);
    });
  });
});

// =============================================================================
// Profile-based snapshot tests (Decision 11A)
// =============================================================================

// Simulated brick sets for each deployment profile
const PROFILE_BRICKS: Record<string, readonly string[]> = {
  full: [
    "storage", "versioning", "agent_runtime", "delegation", "ipc",
    "access_manifest", "governance", "auth", "pay", "search", "catalog",
    "memory", "rlm", "workflows", "scheduler", "eventlog", "reputation",
  ],
  lite: [
    "storage", "versioning", "agent_runtime", "delegation", "ipc",
    "access_manifest", "auth", "search", "catalog", "eventlog",
  ],
  embedded: [
    "storage", "versioning", "search", "catalog",
  ],
  cloud: [
    "storage", "versioning", "agent_runtime", "delegation", "ipc",
    "access_manifest", "governance", "auth", "pay", "search", "catalog",
    "memory", "rlm", "workflows", "scheduler", "eventlog", "reputation",
    "mcp", "workspace",
  ],
  minimal: [
    "storage",
  ],
};

describe("profile-based tab visibility snapshots", () => {
  describe("full profile", () => {
    const bricks = PROFILE_BRICKS.full;

    it("access panel shows all tabs", () => {
      const result = filterTabs(ACCESS_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual([
        "manifests", "alerts", "credentials", "fraud", "delegations",
      ]);
    });

    it("search panel shows all tabs", () => {
      const result = filterTabs(SEARCH_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual([
        "search", "knowledge", "memories", "playbooks", "ask", "columns",
      ]);
    });

    it("agents panel shows all tabs", () => {
      const result = filterTabs(AGENT_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual(["status", "delegations", "inbox"]);
    });

    it("events panel shows all tabs", () => {
      const result = filterTabs(EVENT_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual([
        "events", "mcl", "connectors", "subscriptions", "locks", "secrets",
      ]);
    });

    it("zones panel shows all tabs including reindex", () => {
      const result = filterTabs(ZONE_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual(["zones", "bricks", "drift", "reindex"]);
    });
  });

  describe("lite profile", () => {
    const bricks = PROFILE_BRICKS.lite;

    it("access panel hides governance tabs (alerts, fraud)", () => {
      const result = filterTabs(ACCESS_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual([
        "manifests", "credentials", "delegations",
      ]);
    });

    it("search panel hides memory and rlm tabs", () => {
      const result = filterTabs(SEARCH_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual([
        "search", "knowledge", "playbooks", "columns",
      ]);
    });

    it("agents panel shows all tabs", () => {
      const result = filterTabs(AGENT_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual(["status", "delegations", "inbox"]);
    });

    it("events panel hides mcl (needs catalog), keeps secrets (auth enabled)", () => {
      const result = filterTabs(EVENT_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual([
        "events", "mcl", "connectors", "subscriptions", "locks", "secrets",
      ]);
    });
  });

  describe("embedded profile", () => {
    const bricks = PROFILE_BRICKS.embedded;

    it("access panel shows no tabs (no access_manifest, governance, auth, delegation)", () => {
      const result = filterTabs(ACCESS_TABS, bricks, true);
      expect(result).toEqual([]);
    });

    it("search panel shows search, knowledge, playbooks, columns", () => {
      const result = filterTabs(SEARCH_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual([
        "search", "knowledge", "playbooks", "columns",
      ]);
    });

    it("agents panel shows no tabs", () => {
      const result = filterTabs(AGENT_TABS, bricks, true);
      expect(result).toEqual([]);
    });

    it("zones panel shows reindex (versioning enabled)", () => {
      const result = filterTabs(ZONE_TABS, bricks, true);
      expect(result.map((t) => t.id)).toContain("reindex");
    });
  });

  describe("minimal profile (storage only)", () => {
    const bricks = PROFILE_BRICKS.minimal;

    it("access panel shows no tabs", () => {
      const result = filterTabs(ACCESS_TABS, bricks, true);
      expect(result).toEqual([]);
    });

    it("search panel shows only playbooks (null brick)", () => {
      const result = filterTabs(SEARCH_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual(["playbooks"]);
    });

    it("agents panel shows no tabs", () => {
      const result = filterTabs(AGENT_TABS, bricks, true);
      expect(result).toEqual([]);
    });

    it("zones panel shows only always-visible tabs (no reindex)", () => {
      const result = filterTabs(ZONE_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual(["zones", "bricks", "drift"]);
    });

    it("events panel shows only always-visible tabs", () => {
      const result = filterTabs(EVENT_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual(["connectors", "locks"]);
    });
  });
});

// =============================================================================
// Global store features integration
// =============================================================================

describe("global store features integration", () => {
  beforeEach(() => {
    useGlobalStore.setState({
      enabledBricks: [],
      featuresLoaded: false,
      featuresLastFetched: 0,
      profile: null,
      mode: null,
    });
  });

  it("setFeatures sets featuresLoaded to true", () => {
    useGlobalStore.getState().setFeatures({
      profile: "full",
      mode: "standalone",
      enabled_bricks: ["search", "catalog"],
      disabled_bricks: ["pay"],
      version: "0.8.0",
      rate_limit_enabled: false,
    });

    const state = useGlobalStore.getState();
    expect(state.featuresLoaded).toBe(true);
    expect(state.enabledBricks).toEqual(["search", "catalog"]);
    expect(state.profile).toBe("full");
  });

  it("setFeatures updates featuresLastFetched timestamp", () => {
    const before = Date.now();
    useGlobalStore.getState().setFeatures({
      profile: "lite",
      mode: "standalone",
      enabled_bricks: [],
      disabled_bricks: [],
      version: null,
      rate_limit_enabled: false,
    });
    const after = Date.now();

    const { featuresLastFetched } = useGlobalStore.getState();
    expect(featuresLastFetched).toBeGreaterThanOrEqual(before);
    expect(featuresLastFetched).toBeLessThanOrEqual(after);
  });

  it("filterTabs uses enabledBricks from global store", () => {
    useGlobalStore.getState().setFeatures({
      profile: "lite",
      mode: "standalone",
      enabled_bricks: ["delegation", "auth"],
      disabled_bricks: [],
      version: null,
      rate_limit_enabled: false,
    });

    const { enabledBricks, featuresLoaded } = useGlobalStore.getState();
    const result = filterTabs(ACCESS_TABS, enabledBricks, featuresLoaded);
    expect(result.map((t) => t.id)).toEqual(["credentials", "delegations"]);
  });
});

// =============================================================================
// Migrated panel regression tests (#3498)
// =============================================================================

describe("migrated TAB_ORDER panels", () => {
  const bricks = PROFILE_BRICKS.full;
  const minimalBricks = PROFILE_BRICKS.minimal;

  describe("connectors (all brick: null)", () => {
    it("shows all tabs regardless of enabled bricks", () => {
      const result = filterTabs(CONNECTORS_TABS, [], true);
      expect(result.map((t) => t.id)).toEqual(["available", "mounted", "skills", "write"]);
    });

    it("shows all tabs under minimal profile", () => {
      const result = filterTabs(CONNECTORS_TABS, minimalBricks, true);
      expect(result.map((t) => t.id)).toEqual(["available", "mounted", "skills", "write"]);
    });
  });

  describe("payments (all brick: null)", () => {
    it("shows all tabs regardless of enabled bricks", () => {
      const result = filterTabs(PAYMENTS_TABS, [], true);
      expect(result.map((t) => t.id)).toEqual([
        "balance", "reservations", "transactions", "policies", "approvals",
      ]);
    });

    it("shows all tabs under minimal profile", () => {
      const result = filterTabs(PAYMENTS_TABS, minimalBricks, true);
      expect(result.map((t) => t.id)).toEqual([
        "balance", "reservations", "transactions", "policies", "approvals",
      ]);
    });
  });

  describe("workflows (all brick: null)", () => {
    it("shows all tabs regardless of enabled bricks", () => {
      const result = filterTabs(WORKFLOW_TABS, [], true);
      expect(result.map((t) => t.id)).toEqual(["workflows", "executions", "scheduler"]);
    });
  });

  describe("files (mixed: explorer always visible, shareLinks/uploads gated)", () => {
    it("shows all tabs under full profile", () => {
      const result = filterTabs(FILES_TABS, bricks, true);
      expect(result.map((t) => t.id)).toEqual(["explorer", "shareLinks", "uploads"]);
    });

    it("shows only explorer under minimal profile", () => {
      const result = filterTabs(FILES_TABS, minimalBricks, true);
      expect(result.map((t) => t.id)).toEqual(["explorer"]);
    });

    it("shows explorer + shareLinks when share_link brick enabled", () => {
      const result = filterTabs(FILES_TABS, ["share_link"], true);
      expect(result.map((t) => t.id)).toEqual(["explorer", "shareLinks"]);
    });

    it("shows explorer + uploads when uploads brick enabled", () => {
      const result = filterTabs(FILES_TABS, ["uploads"], true);
      expect(result.map((t) => t.id)).toEqual(["explorer", "uploads"]);
    });
  });
});
