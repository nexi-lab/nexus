/**
 * Zones panel: tabbed layout with Zones list, Bricks health, and Drift report.
 *
 * Keybindings are context-aware — only actions valid for the selected brick's
 * current state are active and displayed in the help bar.
 */

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useZonesStore } from "../../stores/zones-store.js";
import type { ZoneTab } from "../../stores/zones-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { ZoneList } from "./zone-list.js";
import { BrickList } from "./brick-list.js";
import { BrickDetail } from "./brick-detail.js";
import { DriftView } from "./drift-view.js";
import { ReindexStatus } from "./reindex-status.js";
import { ConfirmDialog } from "../../shared/components/confirm-dialog.js";
import { allowedActionsForState } from "../../shared/brick-states.js";

const TAB_ORDER: readonly ZoneTab[] = ["zones", "bricks", "drift", "reindex"];
const TAB_LABELS: Readonly<Record<ZoneTab, string>> = {
  zones: "Zones",
  bricks: "Bricks",
  drift: "Drift",
  reindex: "Reindex",
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
  const mountBrick = useZonesStore((s) => s.mountBrick);
  const unmountBrick = useZonesStore((s) => s.unmountBrick);
  const unregisterBrick = useZonesStore((s) => s.unregisterBrick);
  const remountBrick = useZonesStore((s) => s.remountBrick);
  const resetBrick = useZonesStore((s) => s.resetBrick);
  const setSelectedIndex = useZonesStore((s) => s.setSelectedIndex);
  const setActiveTab = useZonesStore((s) => s.setActiveTab);

  // Confirmation dialog state for destructive unregister action
  const [confirmUnregister, setConfirmUnregister] = useState(false);

  // Currently selected brick (if on bricks tab)
  const selectedBrick = activeTab === "bricks" ? bricks[selectedIndex] ?? null : null;

  // Allowed actions for the selected brick's current state
  const allowed = useMemo(
    () => (selectedBrick ? allowedActionsForState(selectedBrick.state) : new Set<string>()),
    [selectedBrick?.state],
  );

  // Refresh data for the current tab
  const refreshActiveTab = useCallback((): void => {
    if (!client) return;

    if (activeTab === "zones") {
      fetchZones(client);
    } else if (activeTab === "bricks") {
      fetchBricks(client);
    } else if (activeTab === "drift") {
      fetchDrift(client);
    }
  }, [activeTab, client, fetchZones, fetchBricks, fetchDrift]);

  // Auto-fetch data on mount and when tab changes
  useEffect(() => {
    refreshActiveTab();
  }, [refreshActiveTab]);

  // Fetch brick detail when selection changes in bricks tab
  useEffect(() => {
    if (!client || activeTab !== "bricks") return;
    const brick = bricks[selectedIndex];
    if (brick) {
      fetchBrickDetail(brick.name, client);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIndex, bricks, activeTab, client]);

  // Confirmation handlers
  const handleConfirmUnregister = useCallback(() => {
    if (!client || !selectedBrick) return;
    unregisterBrick(selectedBrick.name, client);
    setConfirmUnregister(false);
  }, [client, selectedBrick, unregisterBrick]);

  const handleCancelUnregister = useCallback(() => {
    setConfirmUnregister(false);
  }, []);

  // Build context-aware help text for the bricks tab
  const brickHelpText = useMemo(() => {
    const parts: string[] = ["j/k:navigate", "Tab:switch tab"];
    if (allowed.has("mount")) parts.push("M:mount");
    if (allowed.has("remount")) parts.push("m:remount");
    if (allowed.has("unmount")) parts.push("U:unmount");
    if (allowed.has("unregister")) parts.push("D:unregister");
    if (allowed.has("reset")) parts.push("x:reset");
    parts.push("r:refresh", "q:quit");
    return parts.join("  ");
  }, [allowed]);

  useKeyboard(
    confirmUnregister
      ? {} // ConfirmDialog handles its own keys when visible
      : {
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
          // M (shift+m): Mount — valid for registered/unmounted
          "shift+m": () => {
            if (!client || !selectedBrick || !allowed.has("mount")) return;
            mountBrick(selectedBrick.name, client);
          },
          // U: Unmount — valid for active
          "shift+u": () => {
            if (!client || !selectedBrick || !allowed.has("unmount")) return;
            unmountBrick(selectedBrick.name, client);
          },
          // D: Unregister — valid for unmounted (with confirmation)
          "shift+d": () => {
            if (!client || !selectedBrick || !allowed.has("unregister")) return;
            setConfirmUnregister(true);
          },
          // m: Remount (existing) — valid for unmounted only
          m: () => {
            if (!client || !selectedBrick || !allowed.has("remount")) return;
            remountBrick(selectedBrick.name, client);
          },
          // x: Reset (existing) — valid for failed
          x: () => {
            if (!client || !selectedBrick || !allowed.has("reset")) return;
            resetBrick(selectedBrick.name, client);
          },
          r: () => {
            refreshActiveTab();
          },
        },
  );

  const defaultHelp = "j/k:navigate  Tab:switch tab  r:refresh  q:quit";

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

        {activeTab === "reindex" && <ReindexStatus />}
      </box>

      {/* Context-aware help bar */}
      <box height={1} width="100%">
        <text>
          {activeTab === "bricks" ? brickHelpText : defaultHelp}
        </text>
      </box>

      {/* Unregister confirmation dialog */}
      <ConfirmDialog
        visible={confirmUnregister}
        title="Unregister Brick"
        message={`Permanently unregister "${selectedBrick?.name ?? ""}"? This cannot be undone.`}
        onConfirm={handleConfirmUnregister}
        onCancel={handleCancelUnregister}
      />
    </box>
  );
}
