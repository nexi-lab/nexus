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

// =============================================================================
// Tab configuration
// =============================================================================

const TAB_ORDER: readonly ConnectorsTab[] = [
  "available",
  "mounted",
  "skills",
  "write",
];

const TAB_LABELS: Readonly<Record<ConnectorsTab, string>> = {
  available: "Available",
  mounted: "Mounted",
  skills: "Skills",
  write: "Write",
};

// =============================================================================
// Panel component
// =============================================================================

export default function ConnectorsPanel(): React.ReactNode {
  const client = useApi();
  const overlayActive = useUiStore((s) => s.overlayActive);
  const activeTab = useConnectorsStore((s) => s.activeTab);
  const setActiveTab = useConnectorsStore((s) => s.setActiveTab);

  // Only the panel root handles Tab key for sub-tab cycling.
  // All other keys are delegated to the active sub-tab component.
  useKeyboard(
    overlayActive
      ? {}
      : {
          tab: () => {
            const currentIdx = TAB_ORDER.indexOf(activeTab);
            const nextIdx = (currentIdx + 1) % TAB_ORDER.length;
            const nextTab = TAB_ORDER[nextIdx];
            if (nextTab) {
              setActiveTab(nextTab);
            }
          },
          "shift+tab": () => {
            const currentIdx = TAB_ORDER.indexOf(activeTab);
            const prevIdx = (currentIdx - 1 + TAB_ORDER.length) % TAB_ORDER.length;
            const prevTab = TAB_ORDER[prevIdx];
            if (prevTab) {
              setActiveTab(prevTab);
            }
          },
        },
  );

  if (!client) {
    return <LoadingIndicator message="Connecting..." />;
  }

  return (
    <BrickGate brick="storage">
      <box height="100%" width="100%" flexDirection="column">
        {/* Sub-tab bar */}
        <box height={1} width="100%">
          <text>
            {TAB_ORDER.map((tab) => {
              const label = TAB_LABELS[tab];
              return tab === activeTab ? `[${label}]` : ` ${label} `;
            }).join(" ")}
          </text>
        </box>

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
