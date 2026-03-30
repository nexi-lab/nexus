/**
 * Connectors panel: tabbed layout for Available, Mounted, Skills, and Write views.
 *
 * Sub-tab routing delegates keyboard context to per-tab components (Decision 8A).
 * Gated on "mount" brick availability.
 */

import React, { useEffect } from "react";
import { useConnectorsStore } from "../../stores/connectors-store.js";
import type { ConnectorsTab } from "../../stores/connectors-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useUiStore } from "../../stores/ui-store.js";
import { BrickGate } from "../../shared/components/brick-gate.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { AvailableTab } from "./available-tab.js";
import { MountedTab } from "./mounted-tab.js";
import { SkillsTab } from "./skills-tab.js";
import { WriteTab } from "./write-tab.js";
import { statusColor } from "../../shared/theme.js";
import { useVisibleTabs, type TabDef } from "../../shared/hooks/use-visible-tabs.js";
import { SubTabBar } from "../../shared/components/sub-tab-bar.js";
import { subTabCycleBindings } from "../../shared/components/sub-tab-bar-utils.js";
import { useTabFallback } from "../../shared/hooks/use-tab-fallback.js";

// =============================================================================
// Tab configuration
// =============================================================================

const ALL_TABS: readonly TabDef<ConnectorsTab>[] = [
  { id: "available", label: "Available", brick: null },
  { id: "mounted", label: "Mounted", brick: null },
  { id: "skills", label: "Skills", brick: null },
  { id: "write", label: "Write", brick: null },
];

// =============================================================================
// Panel component
// =============================================================================

export default function ConnectorsPanel(): React.ReactNode {
  const client = useApi();
  const overlayActive = useUiStore((s) => s.overlayActive);
  const activeTab = useConnectorsStore((s) => s.activeTab);
  const setActiveTab = useConnectorsStore((s) => s.setActiveTab);

  const visibleTabs = useVisibleTabs(ALL_TABS);
  useTabFallback(visibleTabs, activeTab, setActiveTab);

  // Only the panel root handles Tab key for sub-tab cycling.
  // All other keys are delegated to the active sub-tab component.
  useKeyboard(
    overlayActive
      ? {}
      : {
          ...subTabCycleBindings(visibleTabs, activeTab, setActiveTab),
        },
  );

  if (!client) {
    return <LoadingIndicator message="Connecting..." />;
  }

  return (
    <BrickGate brick="storage">
      <box height="100%" width="100%" flexDirection="column">
        {/* Sub-tab bar */}
        <SubTabBar tabs={visibleTabs} activeTab={activeTab} />

        {/* Active tab content */}
        <box flexGrow={1}>
          {activeTab === "available" && (
            <AvailableTab client={client} overlayActive={overlayActive} />
          )}
          {activeTab === "mounted" && (
            <MountedTab client={client} overlayActive={overlayActive} />
          )}
          {activeTab === "skills" && (
            <SkillsTab client={client} overlayActive={overlayActive} />
          )}
          {activeTab === "write" && (
            <WriteTab client={client} overlayActive={overlayActive} />
          )}
        </box>
      </box>
    </BrickGate>
  );
}
