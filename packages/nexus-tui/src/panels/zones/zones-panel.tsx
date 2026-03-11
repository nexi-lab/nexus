/**
 * Zones panel: left sidebar with brick list, right pane with tabbed detail views.
 */

import React, { useEffect } from "react";
import { useZonesStore } from "../../stores/zones-store.js";
import type { ZoneTab } from "../../stores/zones-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { BrickList } from "./brick-list.js";
import { BrickDetail } from "./brick-detail.js";
import { HealthView } from "./health-view.js";
import { MountTable } from "./mount-table.js";
import { DriftView } from "./drift-view.js";

const TAB_ORDER: readonly ZoneTab[] = ["overview", "health", "mounts", "drift"];
const TAB_LABELS: Readonly<Record<ZoneTab, string>> = {
  overview: "Overview",
  health: "Health",
  mounts: "Mounts",
  drift: "Drift",
};

export default function ZonesPanel(): React.ReactNode {
  const client = useApi();

  const bricks = useZonesStore((s) => s.bricks);
  const selectedBrick = useZonesStore((s) => s.selectedBrick);
  const selectedIndex = useZonesStore((s) => s.selectedIndex);
  const activeTab = useZonesStore((s) => s.activeTab);
  const isLoading = useZonesStore((s) => s.isLoading);
  const brickHealth = useZonesStore((s) => s.brickHealth);
  const healthLoading = useZonesStore((s) => s.healthLoading);
  const mountPoints = useZonesStore((s) => s.mountPoints);
  const mountsLoading = useZonesStore((s) => s.mountsLoading);
  const driftReport = useZonesStore((s) => s.driftReport);
  const driftLoading = useZonesStore((s) => s.driftLoading);
  const error = useZonesStore((s) => s.error);

  const fetchBricks = useZonesStore((s) => s.fetchBricks);
  const fetchBrickHealth = useZonesStore((s) => s.fetchBrickHealth);
  const fetchMounts = useZonesStore((s) => s.fetchMounts);
  const fetchDrift = useZonesStore((s) => s.fetchDrift);
  const triggerSync = useZonesStore((s) => s.triggerSync);
  const setSelectedIndex = useZonesStore((s) => s.setSelectedIndex);
  const setActiveTab = useZonesStore((s) => s.setActiveTab);

  // Fetch detail data for the active tab
  const refreshDetailView = (): void => {
    if (!client || !selectedBrick) return;

    const brickId = selectedBrick.brick_id;
    if (activeTab === "health") {
      fetchBrickHealth(brickId, client);
    } else if (activeTab === "mounts") {
      fetchMounts(brickId, client);
    } else if (activeTab === "drift") {
      fetchDrift(brickId, client);
    }
  };

  // Auto-fetch brick list on mount
  useEffect(() => {
    if (client) {
      fetchBricks(client);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  // Auto-fetch detail data when brick or tab changes
  useEffect(() => {
    refreshDetailView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedBrick, activeTab, client]);

  useKeyboard({
    j: () => {
      setSelectedIndex(Math.min(selectedIndex + 1, bricks.length - 1));
    },
    down: () => {
      setSelectedIndex(Math.min(selectedIndex + 1, bricks.length - 1));
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
    s: () => {
      if (!client || !selectedBrick) return;
      triggerSync(selectedBrick.brick_id, client);
    },
    r: () => {
      if (!client) return;
      fetchBricks(client);
      refreshDetailView();
    },
    return: () => {
      const brick = bricks[selectedIndex];
      if (brick) {
        setSelectedIndex(selectedIndex);
      }
    },
  });

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Main content */}
      <box flexGrow={1} flexDirection="row">
        {/* Left sidebar: brick list (30%) */}
        <box width="30%" height="100%" borderStyle="single" flexDirection="column">
          <box height={1} width="100%">
            <text>{"--- Zones & Bricks ---"}</text>
          </box>

          <BrickList
            bricks={bricks}
            selectedIndex={selectedIndex}
            loading={isLoading}
          />
        </box>

        {/* Right pane: detail views (70%) */}
        <box width="70%" height="100%" flexDirection="column">
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

          {/* Detail content */}
          <box flexGrow={1} borderStyle="single">
            {activeTab === "overview" && (
              <BrickDetail brick={selectedBrick} />
            )}
            {activeTab === "health" && (
              <HealthView health={brickHealth} loading={healthLoading} />
            )}
            {activeTab === "mounts" && (
              <MountTable mounts={mountPoints} loading={mountsLoading} />
            )}
            {activeTab === "drift" && (
              <DriftView drift={driftReport} loading={driftLoading} />
            )}
          </box>
        </box>
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {"j/k:navigate  Tab:switch tab  s:sync  r:refresh  Enter:select  q:quit"}
        </text>
      </box>
    </box>
  );
}
