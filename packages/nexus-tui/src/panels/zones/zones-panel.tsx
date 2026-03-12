/**
 * Zones panel: tabbed layout with Zones list, Bricks health, and Drift report.
 */

import React, { useEffect } from "react";
import { useZonesStore } from "../../stores/zones-store.js";
import type { ZoneTab } from "../../stores/zones-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { ZoneList } from "./zone-list.js";
import { BrickList } from "./brick-list.js";
import { BrickDetail } from "./brick-detail.js";
import { DriftView } from "./drift-view.js";

const TAB_ORDER: readonly ZoneTab[] = ["zones", "bricks", "drift"];
const TAB_LABELS: Readonly<Record<ZoneTab, string>> = {
  zones: "Zones",
  bricks: "Bricks",
  drift: "Drift",
};

export default function ZonesPanel(): React.ReactNode {
  const client = useApi();

  const zones = useZonesStore((s) => s.zones);
  const zonesLoading = useZonesStore((s) => s.zonesLoading);
  const bricks = useZonesStore((s) => s.bricks);
  const bricksHealth = useZonesStore((s) => s.bricksHealth);
  const selectedIndex = useZonesStore((s) => s.selectedIndex);
  const activeTab = useZonesStore((s) => s.activeTab);
  const isLoading = useZonesStore((s) => s.isLoading);
  const brickDetail = useZonesStore((s) => s.brickDetail);
  const detailLoading = useZonesStore((s) => s.detailLoading);
  const driftReport = useZonesStore((s) => s.driftReport);
  const driftLoading = useZonesStore((s) => s.driftLoading);
  const error = useZonesStore((s) => s.error);

  const fetchZones = useZonesStore((s) => s.fetchZones);
  const fetchBricks = useZonesStore((s) => s.fetchBricks);
  const fetchBrickDetail = useZonesStore((s) => s.fetchBrickDetail);
  const fetchDrift = useZonesStore((s) => s.fetchDrift);
  const remountBrick = useZonesStore((s) => s.remountBrick);
  const resetBrick = useZonesStore((s) => s.resetBrick);
  const setSelectedIndex = useZonesStore((s) => s.setSelectedIndex);
  const setActiveTab = useZonesStore((s) => s.setActiveTab);

  // Refresh data for the current tab
  const refreshActiveTab = (): void => {
    if (!client) return;

    if (activeTab === "zones") {
      fetchZones(client);
    } else if (activeTab === "bricks") {
      fetchBricks(client);
    } else if (activeTab === "drift") {
      fetchDrift(client);
    }
  };

  // Auto-fetch data on mount and when tab changes
  useEffect(() => {
    refreshActiveTab();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, client]);

  // Fetch brick detail when selection changes in bricks tab
  useEffect(() => {
    if (!client || activeTab !== "bricks") return;
    const brick = bricks[selectedIndex];
    if (brick) {
      fetchBrickDetail(brick.name, client);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIndex, bricks, activeTab, client]);

  useKeyboard({
    j: () => {
      const maxLen = activeTab === "zones" ? zones.length : bricks.length;
      setSelectedIndex(Math.min(selectedIndex + 1, maxLen - 1));
    },
    down: () => {
      const maxLen = activeTab === "zones" ? zones.length : bricks.length;
      setSelectedIndex(Math.min(selectedIndex + 1, maxLen - 1));
    },
    k: () => {
      setSelectedIndex(Math.max(selectedIndex - 1, 0));
    },
    up: () => {
      setSelectedIndex(Math.max(selectedIndex - 1, 0));
    },
    tab: () => {
      const currentIdx = TAB_ORDER.indexOf(activeTab);
      const nextIdx = (currentIdx + 1) % TAB_ORDER.length;
      const nextTab = TAB_ORDER[nextIdx];
      if (nextTab) {
        setActiveTab(nextTab);
      }
    },
    m: () => {
      if (!client || activeTab !== "bricks") return;
      const brick = bricks[selectedIndex];
      if (brick) {
        remountBrick(brick.name, client);
      }
    },
    x: () => {
      if (!client || activeTab !== "bricks") return;
      const brick = bricks[selectedIndex];
      if (brick) {
        resetBrick(brick.name, client);
      }
    },
    r: () => {
      refreshActiveTab();
    },
  });

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

      {/* Error display */}
      {error && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {/* Main content */}
      <box flexGrow={1} flexDirection="row">
        {activeTab === "zones" && (
          <ZoneList
            zones={zones}
            selectedIndex={selectedIndex}
            loading={zonesLoading}
          />
        )}

        {activeTab === "bricks" && (
          <>
            {/* Left sidebar: brick list (30%) */}
            <box width="30%" height="100%" borderStyle="single" flexDirection="column">
              <box height={1} width="100%">
                <text>
                  {bricksHealth
                    ? `--- Bricks (${bricksHealth.active}/${bricksHealth.total} active, ${bricksHealth.failed} failed) ---`
                    : "--- Bricks ---"}
                </text>
              </box>

              <BrickList
                bricks={bricks}
                selectedIndex={selectedIndex}
                loading={isLoading}
              />
            </box>

            {/* Right pane: brick detail (70%) */}
            <box width="70%" height="100%" borderStyle="single">
              <BrickDetail brick={brickDetail} loading={detailLoading} />
            </box>
          </>
        )}

        {activeTab === "drift" && (
          <DriftView drift={driftReport} loading={driftLoading} />
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {"j/k:navigate  Tab:switch tab  m:remount  x:reset  r:refresh  q:quit"}
        </text>
      </box>
    </box>
  );
}
