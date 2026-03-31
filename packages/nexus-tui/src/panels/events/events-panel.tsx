/**
 * Infrastructure & Events panel — thin tab router.
 *
 * Each tab is a self-contained sub-component that owns its own store
 * subscriptions, keybindings, and rendering.
 *
 * Refactored from 679-line monolith (Issue 2A: split into per-tab sub-panels).
 */

import React from "react";
import { useUiStore } from "../../stores/ui-store.js";
import { useInfraStore } from "../../stores/infra-store.js";
import type { InfraTab } from "../../stores/infra-store.js";
import { useVisibleTabs } from "../../shared/hooks/use-visible-tabs.js";
import { SubTabBar } from "../../shared/components/sub-tab-bar.js";
import { subTabCycleBindings } from "../../shared/components/sub-tab-bar-utils.js";
import { useTabFallback } from "../../shared/hooks/use-tab-fallback.js";
import { Tooltip } from "../../shared/components/tooltip.js";
import { EVENTS_TABS } from "../../shared/navigation.js";

// Per-tab sub-components
import { EventsTab } from "./events-tab.js";
import { MclTab } from "./mcl-tab.js";
import { ReplayTab } from "./replay-tab.js";
import { OperationsTabWrapper } from "./operations-tab-wrapper.js";
import { ConnectorsTab } from "./connectors-tab.js";
import { SubscriptionsTab } from "./subscriptions-tab.js";
import { LocksTab } from "./locks-tab.js";
import { SecretsTab } from "./secrets-tab.js";
import { AuditTab } from "./audit-tab.js";

type PanelTab = "events" | "mcl" | "replay" | "operations" | "audit" | InfraTab;

export default function EventsPanel(): React.ReactNode {
  const overlayActive = useUiStore((s) => s.overlayActive);
  const visibleTabs = useVisibleTabs(EVENTS_TABS);

  const [activeTab, setActiveTab] = React.useState<PanelTab>("events");
  useTabFallback(visibleTabs, activeTab, setActiveTab);

  // Sync infra tab state for panels that use useInfraStore
  const setInfraTab = useInfraStore((s) => s.setActiveTab);
  React.useEffect(() => {
    if (activeTab !== "events" && activeTab !== "mcl" && activeTab !== "replay" && activeTab !== "operations" && activeTab !== "audit") {
      setInfraTab(activeTab as InfraTab);
    }
  }, [activeTab, setInfraTab]);

  // Tab cycling bindings — passed to each sub-tab so they can compose them
  const tabBindings = subTabCycleBindings(visibleTabs, activeTab, setActiveTab);

  const infraError = useInfraStore((s) => s.error);

  return (
    <box height="100%" width="100%" flexDirection="column">
      <Tooltip tooltipKey="events-panel" message="Tip: Press ? for keybinding help" />
      <SubTabBar tabs={visibleTabs} activeTab={activeTab} />

      {/* Infra error (non-SSE tabs) */}
      {infraError && activeTab !== "events" && activeTab !== "mcl" && activeTab !== "replay" && (
        <box height={1} width="100%">
          <text>{`Error: ${infraError}`}</text>
        </box>
      )}

      {/* Active tab */}
      {activeTab === "events" && <EventsTab tabBindings={tabBindings} overlayActive={overlayActive} />}
      {activeTab === "mcl" && <MclTab tabBindings={tabBindings} overlayActive={overlayActive} />}
      {activeTab === "replay" && <ReplayTab tabBindings={tabBindings} overlayActive={overlayActive} />}
      {activeTab === "operations" && <OperationsTabWrapper tabBindings={tabBindings} overlayActive={overlayActive} />}
      {activeTab === "connectors" && <ConnectorsTab tabBindings={tabBindings} overlayActive={overlayActive} />}
      {activeTab === "subscriptions" && <SubscriptionsTab tabBindings={tabBindings} overlayActive={overlayActive} />}
      {activeTab === "locks" && <LocksTab tabBindings={tabBindings} overlayActive={overlayActive} />}
      {activeTab === "secrets" && <SecretsTab tabBindings={tabBindings} overlayActive={overlayActive} />}
      {activeTab === "audit" && <AuditTab tabBindings={tabBindings} overlayActive={overlayActive} />}
    </box>
  );
}
