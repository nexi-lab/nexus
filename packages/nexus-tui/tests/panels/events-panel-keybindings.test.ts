/**
 * Tests for events-panel-keybindings (#3623).
 *
 * Covers:
 * - FilterMode type and transitions
 * - getEventsHelpText: correct text per tab/mode
 * - getEventsKeyBindings: overlay guard, filter mode, normal mode
 * - handleEventsUnhandledKey: character accumulation in filter modes
 * - formatEventData: JSON pretty-print and fallback
 */

import { describe, it, expect, mock } from "bun:test";
import {
  type FilterMode,
  getEventsHelpText,
  getEventsKeyBindings,
  handleEventsUnhandledKey,
  formatEventData,
  type EventsBindingContext,
} from "../../src/panels/events/events-panel-keybindings.js";

// =============================================================================
// Minimal context factory
// =============================================================================

function makeCtx(overrides: Partial<EventsBindingContext> = {}): EventsBindingContext {
  return {
    activeTab: "events",
    visibleTabs: [
      { id: "events", label: "Events", brick: "eventlog" },
      { id: "connectors", label: "Connectors", brick: null },
      { id: "subscriptions", label: "Subscriptions", brick: "eventlog" },
      { id: "locks", label: "Locks", brick: null },
    ],
    setActiveTab: mock(() => {}),
    filterMode: "none",
    filterBuffer: "",
    setFilterMode: mock(() => {}),
    setFilterBuffer: mock(() => {}),
    events: [],
    selectedEventIndex: -1,
    setSelectedEventIndex: mock(() => {}),
    expandedEventIndex: null,
    setExpandedEventIndex: mock(() => {}),
    filters: { eventType: null, search: null },
    setFilter: mock(() => {}),
    clearEvents: mock(() => {}),
    copy: mock(() => {}),
    reconnect: mock(() => {}),
    mclUrnFilter: "",
    setMclUrnFilter: mock(() => {}),
    mclAspectFilter: "",
    setMclAspectFilter: mock(() => {}),
    clearReplay: mock(() => {}),
    fetchReplay: mock(async () => {}),
    replayTypeFilter: "",
    setReplayTypeFilter: mock(() => {}),
    clearEventReplay: mock(() => {}),
    fetchEventReplay: mock(async () => {}),
    connectors: [],
    selectedConnectorIndex: 0,
    setSelectedConnectorIndex: mock(() => {}),
    connectorDetailView: false,
    setConnectorDetailView: mock(() => {}),
    fetchConnectors: mock(async () => {}),
    fetchConnectorCapabilities: mock(async () => {}),
    subscriptions: [],
    selectedSubscriptionIndex: 0,
    setSelectedSubscriptionIndex: mock(() => {}),
    deleteSubscription: mock(async () => {}),
    testSubscription: mock(async () => {}),
    fetchSubscriptions: mock(async () => {}),
    locks: [],
    selectedLockIndex: 0,
    setSelectedLockIndex: mock(() => {}),
    acquireLock: mock(async () => {}),
    releaseLock: mock(async () => {}),
    extendLock: mock(async () => {}),
    fetchLocks: mock(async () => {}),
    secretsFilter: "",
    setSecretsFilter: mock(() => {}),
    fetchSecretAudit: mock(async () => {}),
    operations: [],
    selectedOperationIndex: 0,
    setSelectedOperationIndex: mock(() => {}),
    fetchOperations: mock(async () => {}),
    auditTransactions: [],
    selectedAuditIndex: 0,
    setSelectedAuditIndex: mock(() => {}),
    auditHasMore: false,
    auditNextCursor: null,
    fetchAuditTransactions: mock(async () => {}),
    apiClient: null,
    confirm: mock(async () => true),
    ...overrides,
  };
}

// =============================================================================
// getEventsHelpText
// =============================================================================

describe("getEventsHelpText", () => {
  it("returns filter mode help when filterMode is not none", () => {
    const modes: FilterMode[] = ["type", "search", "mcl_urn", "mcl_aspect", "acquire_path", "replay_filter", "secrets_filter"];
    for (const mode of modes) {
      const text = getEventsHelpText(mode, "events", false);
      expect(text).toContain("Enter:apply");
      expect(text).toContain("Escape:cancel");
    }
  });

  it("returns normal help for events tab", () => {
    const text = getEventsHelpText("none", "events", false);
    expect(text).toContain("f:type filter");
    expect(text).toContain("Tab:tab");
  });

  it("returns connector detail help when in detail view", () => {
    const text = getEventsHelpText("none", "connectors", true);
    expect(text).toContain("Escape:back");
  });

  it("returns connector nav help when not in detail view", () => {
    const text = getEventsHelpText("none", "connectors", false);
    expect(text).toContain("j/k:navigate");
    expect(text).toContain("Enter:detail");
  });

  it("returns help for each tab", () => {
    const tabs = ["events", "mcl", "replay", "connectors", "subscriptions", "locks", "secrets", "operations", "audit"] as const;
    for (const tab of tabs) {
      const text = getEventsHelpText("none", tab, false);
      expect(text.length).toBeGreaterThan(0);
    }
  });
});

// =============================================================================
// getEventsKeyBindings — overlay guard
// =============================================================================

describe("getEventsKeyBindings — overlay guard", () => {
  it("returns empty bindings when overlay is active", () => {
    const ctx = makeCtx();
    const bindings = getEventsKeyBindings(true, ctx);
    expect(Object.keys(bindings)).toHaveLength(0);
  });

  it("returns bindings when overlay is not active", () => {
    const ctx = makeCtx();
    const bindings = getEventsKeyBindings(false, ctx);
    expect(Object.keys(bindings).length).toBeGreaterThan(0);
  });
});

// =============================================================================
// getEventsKeyBindings — filter mode
// =============================================================================

describe("getEventsKeyBindings — filter mode", () => {
  it("escape cancels the filter", () => {
    const setFilterMode = mock(() => {});
    const setFilterBuffer = mock(() => {});
    const ctx = makeCtx({ filterMode: "type", setFilterMode, setFilterBuffer });
    const bindings = getEventsKeyBindings(false, ctx);

    bindings["escape"]!();

    expect(setFilterMode).toHaveBeenCalledWith("none");
    expect(setFilterBuffer).toHaveBeenCalledWith("");
  });

  it("return applies type filter", () => {
    const setFilter = mock(() => {});
    const setFilterMode = mock(() => {});
    const setFilterBuffer = mock(() => {});
    const ctx = makeCtx({
      filterMode: "type",
      filterBuffer: "login",
      setFilter,
      setFilterMode,
      setFilterBuffer,
    });
    const bindings = getEventsKeyBindings(false, ctx);

    bindings["return"]!();

    expect(setFilter).toHaveBeenCalledWith({ eventType: "login" });
    expect(setFilterMode).toHaveBeenCalledWith("none");
  });

  it("return clears type filter when buffer is empty", () => {
    const setFilter = mock(() => {});
    const ctx = makeCtx({ filterMode: "type", filterBuffer: "", setFilter });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["return"]!();
    expect(setFilter).toHaveBeenCalledWith({ eventType: null });
  });

  it("return applies search filter", () => {
    const setFilter = mock(() => {});
    const ctx = makeCtx({ filterMode: "search", filterBuffer: "foo", setFilter });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["return"]!();
    expect(setFilter).toHaveBeenCalledWith({ search: "foo" });
  });

  it("return applies mcl_urn filter", () => {
    const setMclUrnFilter = mock(() => {});
    const ctx = makeCtx({ filterMode: "mcl_urn", filterBuffer: "urn:nexus:foo", setMclUrnFilter });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["return"]!();
    expect(setMclUrnFilter).toHaveBeenCalledWith("urn:nexus:foo");
  });

  it("backspace deletes last char from buffer", () => {
    const setFilterBuffer = mock(() => {});
    const ctx = makeCtx({ filterMode: "type", setFilterBuffer });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["backspace"]!();
    // setFilterBuffer is called with a function (updater) — check it was called
    expect(setFilterBuffer).toHaveBeenCalled();
    // Verify the updater removes the last character
    const updater = (setFilterBuffer as ReturnType<typeof mock>).mock.calls[0]![0] as (prev: string) => string;
    expect(updater("hello")).toBe("hell");
    expect(updater("a")).toBe("");
    expect(updater("")).toBe("");
  });

  it("only provides escape, return, backspace bindings in filter mode", () => {
    const ctx = makeCtx({ filterMode: "search" });
    const bindings = getEventsKeyBindings(false, ctx);
    const keys = Object.keys(bindings);
    expect(keys).toContain("escape");
    expect(keys).toContain("return");
    expect(keys).toContain("backspace");
    // Normal mode keys should not appear
    expect(keys).not.toContain("j");
    expect(keys).not.toContain("f");
    expect(keys).not.toContain("r");
  });
});

// =============================================================================
// getEventsKeyBindings — normal mode
// =============================================================================

describe("getEventsKeyBindings — normal mode", () => {
  it("includes standard tab cycling bindings", () => {
    const ctx = makeCtx();
    const bindings = getEventsKeyBindings(false, ctx);
    expect(bindings["tab"]).toBeDefined();
    expect(bindings["shift+tab"]).toBeDefined();
  });

  it("tab cycles forward through visible tabs", () => {
    const setActiveTab = mock(() => {});
    const ctx = makeCtx({ activeTab: "events", setActiveTab });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["tab"]!();
    expect(setActiveTab).toHaveBeenCalledWith("connectors");
  });

  it("shift+tab cycles backward through visible tabs", () => {
    const setActiveTab = mock(() => {});
    const ctx = makeCtx({ activeTab: "connectors", setActiveTab });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["shift+tab"]!();
    expect(setActiveTab).toHaveBeenCalledWith("events");
  });

  it("f enters type filter mode on events tab", () => {
    const setFilterMode = mock(() => {});
    const setFilterBuffer = mock(() => {});
    const ctx = makeCtx({ activeTab: "events", setFilterMode, setFilterBuffer });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["f"]!();
    expect(setFilterMode).toHaveBeenCalledWith("type");
  });

  it("s enters search filter mode on events tab", () => {
    const setFilterMode = mock(() => {});
    const ctx = makeCtx({ activeTab: "events", setFilterMode });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["s"]!();
    expect(setFilterMode).toHaveBeenCalledWith("search");
  });

  it("f enters mcl_urn filter mode on mcl tab", () => {
    const setFilterMode = mock(() => {});
    const ctx = makeCtx({ activeTab: "mcl", setFilterMode });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["f"]!();
    expect(setFilterMode).toHaveBeenCalledWith("mcl_urn");
  });

  it("c clears events on events tab", () => {
    const clearEvents = mock(() => {});
    const ctx = makeCtx({ activeTab: "events", clearEvents });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["c"]!();
    expect(clearEvents).toHaveBeenCalled();
  });

  it("c does nothing on non-events tabs", () => {
    const clearEvents = mock(() => {});
    const ctx = makeCtx({ activeTab: "subscriptions", clearEvents });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["c"]!();
    expect(clearEvents).not.toHaveBeenCalled();
  });

  it("d deletes the selected subscription on subscriptions tab", () => {
    const deleteSubscription = mock(async () => {});
    const mockClient = {} as Parameters<typeof makeCtx>[0]["apiClient"];
    const ctx = makeCtx({
      activeTab: "subscriptions",
      subscriptions: [{ subscription_id: "sub-1", event_type: "test", endpoint: "https://example.com", status: "active", filter: null, created_at: "2024-01-01", last_triggered: null, trigger_count: 0 }],
      selectedSubscriptionIndex: 0,
      deleteSubscription,
      apiClient: mockClient,
    });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["d"]!();
    expect(deleteSubscription).toHaveBeenCalledWith("sub-1", mockClient);
  });

  it("a enters acquire_path mode on locks tab", () => {
    const setFilterMode = mock(() => {});
    const ctx = makeCtx({ activeTab: "locks", setFilterMode });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["a"]!();
    expect(setFilterMode).toHaveBeenCalledWith("acquire_path");
  });

  it("escape clears expanded event index when event is expanded", () => {
    const setExpandedEventIndex = mock(() => {});
    const ctx = makeCtx({ expandedEventIndex: 2, setExpandedEventIndex });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["escape"]!();
    expect(setExpandedEventIndex).toHaveBeenCalledWith(null);
  });

  it("escape goes back from connector detail view", () => {
    const setConnectorDetailView = mock(() => {});
    const ctx = makeCtx({ activeTab: "connectors", connectorDetailView: true, setConnectorDetailView });
    const bindings = getEventsKeyBindings(false, ctx);
    bindings["escape"]!();
    expect(setConnectorDetailView).toHaveBeenCalledWith(false);
  });
});

// =============================================================================
// handleEventsUnhandledKey
// =============================================================================

describe("handleEventsUnhandledKey", () => {
  it("does nothing in none filter mode", () => {
    const setFilterBuffer = mock(() => {});
    handleEventsUnhandledKey("none", setFilterBuffer, "a");
    expect(setFilterBuffer).not.toHaveBeenCalled();
  });

  it("appends single character in filter mode", () => {
    const setFilterBuffer = mock(() => {});
    handleEventsUnhandledKey("type", setFilterBuffer, "x");
    expect(setFilterBuffer).toHaveBeenCalled();
    const updater = (setFilterBuffer as ReturnType<typeof mock>).mock.calls[0]![0] as (prev: string) => string;
    expect(updater("hel")).toBe("helx");
  });

  it("appends space when keyName is 'space'", () => {
    const setFilterBuffer = mock(() => {});
    handleEventsUnhandledKey("search", setFilterBuffer, "space");
    expect(setFilterBuffer).toHaveBeenCalled();
    const updater = (setFilterBuffer as ReturnType<typeof mock>).mock.calls[0]![0] as (prev: string) => string;
    expect(updater("hello")).toBe("hello ");
  });

  it("ignores multi-char key names that are not 'space'", () => {
    const setFilterBuffer = mock(() => {});
    handleEventsUnhandledKey("type", setFilterBuffer, "ctrl+a");
    expect(setFilterBuffer).not.toHaveBeenCalled();
  });

  it("works in all non-none filter modes", () => {
    const modes: FilterMode[] = ["type", "search", "mcl_urn", "mcl_aspect", "acquire_path", "replay_filter", "secrets_filter"];
    for (const mode of modes) {
      const setFilterBuffer = mock(() => {});
      handleEventsUnhandledKey(mode, setFilterBuffer, "a");
      expect(setFilterBuffer).toHaveBeenCalled();
    }
  });
});

// =============================================================================
// formatEventData
// =============================================================================

describe("formatEventData", () => {
  it("returns empty string for empty input", () => {
    expect(formatEventData("")).toBe("");
  });

  it("pretty-prints valid JSON", () => {
    const result = formatEventData('{"key":"value"}');
    expect(result).toContain('"key"');
    expect(result).toContain('"value"');
    // Ensure indentation (pretty-print, not minified)
    expect(result).toContain("\n");
  });

  it("returns raw string for non-JSON input", () => {
    const raw = "plain text event data";
    expect(formatEventData(raw)).toBe(raw);
  });

  it("handles nested JSON objects", () => {
    const input = '{"outer":{"inner":42}}';
    const result = formatEventData(input);
    expect(result).toContain('"outer"');
    expect(result).toContain('"inner"');
    expect(result).toContain("42");
  });

  it("handles JSON arrays", () => {
    const input = '["a","b","c"]';
    const result = formatEventData(input);
    expect(result).toContain('"a"');
  });

  it("handles JSON with null values", () => {
    const input = '{"key":null}';
    const result = formatEventData(input);
    expect(result).toContain("null");
  });

  it("does not throw on malformed JSON", () => {
    expect(() => formatEventData("{broken json")).not.toThrow();
    expect(formatEventData("{broken json")).toBe("{broken json");
  });
});
