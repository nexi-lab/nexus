/**
 * Access Control panel: tabbed layout for manifests, alerts, reputation, credentials.
 *
 * Press 'p' to open the permission checker form (pre-filled with the selected manifest).
 */

import React, { useState, useEffect } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import type { AccessTab } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { ManifestList } from "./manifest-list.js";
import { AlertList } from "./alert-list.js";
import { ReputationView } from "./reputation-view.js";
import { CredentialList } from "./credential-list.js";
import { DisputeList } from "./dispute-list.js";
import { PermissionChecker } from "./permission-checker.js";

const TAB_ORDER: readonly AccessTab[] = ["manifests", "alerts", "reputation", "credentials", "disputes"];
const TAB_LABELS: Readonly<Record<AccessTab, string>> = {
  manifests: "Manifests",
  alerts: "Alerts",
  reputation: "Reputation",
  credentials: "Credentials",
  disputes: "Disputes",
};

export default function AccessPanel(): React.ReactNode {
  const client = useApi();
  const [permissionCheckerOpen, setPermissionCheckerOpen] = useState(false);

  const manifests = useAccessStore((s) => s.manifests);
  const selectedManifestIndex = useAccessStore((s) => s.selectedManifestIndex);
  const manifestsLoading = useAccessStore((s) => s.manifestsLoading);
  const lastPermissionCheck = useAccessStore((s) => s.lastPermissionCheck);
  const permissionCheckLoading = useAccessStore((s) => s.permissionCheckLoading);
  const alerts = useAccessStore((s) => s.alerts);
  const alertsLoading = useAccessStore((s) => s.alertsLoading);
  const leaderboard = useAccessStore((s) => s.leaderboard);
  const leaderboardLoading = useAccessStore((s) => s.leaderboardLoading);
  const credentials = useAccessStore((s) => s.credentials);
  const credentialsLoading = useAccessStore((s) => s.credentialsLoading);
  const disputes = useAccessStore((s) => s.disputes);
  const disputesLoading = useAccessStore((s) => s.disputesLoading);
  const selectedDisputeIndex = useAccessStore((s) => s.selectedDisputeIndex);
  const activeTab = useAccessStore((s) => s.activeTab);
  const error = useAccessStore((s) => s.error);

  const fetchManifests = useAccessStore((s) => s.fetchManifests);
  const fetchAlerts = useAccessStore((s) => s.fetchAlerts);
  const fetchLeaderboard = useAccessStore((s) => s.fetchLeaderboard);
  const fetchCredentials = useAccessStore((s) => s.fetchCredentials);
  const setActiveTab = useAccessStore((s) => s.setActiveTab);
  const setSelectedManifestIndex = useAccessStore((s) => s.setSelectedManifestIndex);
  const setSelectedDisputeIndex = useAccessStore((s) => s.setSelectedDisputeIndex);

  // Refresh current view based on active tab
  const refreshCurrentView = (): void => {
    if (!client) return;

    if (activeTab === "manifests") {
      fetchManifests(client);
    } else if (activeTab === "alerts") {
      fetchAlerts(client);
    } else if (activeTab === "reputation") {
      fetchLeaderboard(client);
    } else if (activeTab === "credentials") {
      // Credentials require an agent_id; use selected manifest's agent_id if available
      const selected = manifests[selectedManifestIndex];
      if (selected) {
        fetchCredentials(selected.agent_id, client);
      }
    }
  };

  // Auto-fetch when tab changes
  useEffect(() => {
    refreshCurrentView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, client]);

  useKeyboard({
    j: () => {
      if (activeTab === "manifests") {
        setSelectedManifestIndex(
          Math.min(selectedManifestIndex + 1, manifests.length - 1),
        );
      } else if (activeTab === "disputes") {
        setSelectedDisputeIndex(
          Math.min(selectedDisputeIndex + 1, disputes.length - 1),
        );
      }
    },
    down: () => {
      if (activeTab === "manifests") {
        setSelectedManifestIndex(
          Math.min(selectedManifestIndex + 1, manifests.length - 1),
        );
      } else if (activeTab === "disputes") {
        setSelectedDisputeIndex(
          Math.min(selectedDisputeIndex + 1, disputes.length - 1),
        );
      }
    },
    k: () => {
      if (activeTab === "manifests") {
        setSelectedManifestIndex(Math.max(selectedManifestIndex - 1, 0));
      } else if (activeTab === "disputes") {
        setSelectedDisputeIndex(Math.max(selectedDisputeIndex - 1, 0));
      }
    },
    up: () => {
      if (activeTab === "manifests") {
        setSelectedManifestIndex(Math.max(selectedManifestIndex - 1, 0));
      } else if (activeTab === "disputes") {
        setSelectedDisputeIndex(Math.max(selectedDisputeIndex - 1, 0));
      }
    },
    tab: () => {
      const currentIdx = TAB_ORDER.indexOf(activeTab);
      const nextIdx = (currentIdx + 1) % TAB_ORDER.length;
      const nextTab = TAB_ORDER[nextIdx];
      if (nextTab) {
        setActiveTab(nextTab);
      }
    },
    r: () => refreshCurrentView(),
    p: () => {
      if (!permissionCheckerOpen) {
        setPermissionCheckerOpen(true);
      }
    },
  });

  // Derive the initial manifest ID from the selected manifest
  const selectedManifest = manifests[selectedManifestIndex];
  const initialManifestId = selectedManifest?.manifest_id ?? "";

  if (permissionCheckerOpen) {
    return (
      <box height="100%" width="100%" flexDirection="column">
        {/* Tab bar */}
        <box height={1} width="100%">
          <text>
            {TAB_ORDER.map((tab) => {
              const label = TAB_LABELS[tab];
              return tab === activeTab ? `[${label}]` : ` ${label} `;
            }).join(" ")}
            {" | Permission Checker"}
          </text>
        </box>

        {/* Permission checker form */}
        <box flexGrow={1} borderStyle="single">
          <PermissionChecker
            initialManifestId={initialManifestId}
            lastResult={lastPermissionCheck}
            loading={permissionCheckLoading}
            onClose={() => setPermissionCheckerOpen(false)}
          />
        </box>
      </box>
    );
  }

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

      {/* Permission evaluation result */}
      {lastPermissionCheck && (
        <box height={1} width="100%">
          <text>
            {`Evaluate: tool=${lastPermissionCheck.tool_name} permission=${lastPermissionCheck.permission} agent=${lastPermissionCheck.agent_id} manifest=${lastPermissionCheck.manifest_id}`}
          </text>
        </box>
      )}

      {/* Error display */}
      {error && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {/* Detail content */}
      <box flexGrow={1} borderStyle="single">
        {activeTab === "manifests" && (
          <ManifestList
            manifests={manifests}
            selectedIndex={selectedManifestIndex}
            loading={manifestsLoading}
          />
        )}
        {activeTab === "alerts" && (
          <AlertList
            alerts={alerts}
            loading={alertsLoading}
          />
        )}
        {activeTab === "reputation" && (
          <ReputationView
            leaderboard={leaderboard}
            leaderboardLoading={leaderboardLoading}
          />
        )}
        {activeTab === "credentials" && (
          <CredentialList
            credentials={credentials}
            loading={credentialsLoading}
          />
        )}
        {activeTab === "disputes" && (
          <DisputeList
            disputes={disputes}
            selectedIndex={selectedDisputeIndex}
            loading={disputesLoading}
          />
        )}
      </box>

      {/* Scope note: tuple browser via manifests; proof tree and namespace pending backend */}
      <box height={1} width="100%">
        <text>{"Tuples: via Manifests tab  |  Proof tree / Namespace editor: pending backend API"}</text>
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {"j/k:navigate  Tab:switch tab  p:permission check  r:refresh  q:quit"}
        </text>
      </box>
    </box>
  );
}
