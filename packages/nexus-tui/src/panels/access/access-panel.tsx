/**
 * Access Control panel: tabbed layout for manifests, alerts, reputation,
 * credentials, disputes, and fraud scores.
 *
 * Key bindings:
 *   j/k or up/down : navigate within lists
 *   Tab            : cycle tabs
 *   Enter          : manifests → fetch detail (tuple entries)
 *   p              : open permission checker
 *   f              : (disputes tab) file a new dispute
 *   g              : (disputes tab) look up dispute by ID
 *   Shift+R        : (disputes tab) resolve selected / (alerts tab) resolve selected
 *   c              : (fraud tab) compute fraud scores
 *   r              : refresh current tab
 */

import React, { useState, useEffect } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import type { AccessTab } from "../../stores/access-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { ManifestList } from "./manifest-list.js";
import { AlertList } from "./alert-list.js";
import { ReputationView } from "./reputation-view.js";
import { CredentialList } from "./credential-list.js";
import { DisputeList } from "./dispute-list.js";
import { FraudScoreView } from "./fraud-score-view.js";
import { PermissionChecker } from "./permission-checker.js";
import { DisputeFiler } from "./dispute-filer.js";
import { DisputeLookup } from "./dispute-lookup.js";

const TAB_ORDER: readonly AccessTab[] = [
  "manifests",
  "alerts",
  "reputation",
  "credentials",
  "disputes",
  "fraud",
];
const TAB_LABELS: Readonly<Record<AccessTab, string>> = {
  manifests: "Manifests",
  alerts: "Alerts",
  reputation: "Reputation",
  credentials: "Credentials",
  disputes: "Disputes",
  fraud: "Fraud",
};

type OverlayMode = "none" | "permissionChecker" | "disputeFiler" | "disputeLookup";

export default function AccessPanel(): React.ReactNode {
  const client = useApi();
  const [overlay, setOverlay] = useState<OverlayMode>("none");

  // Zone for fraud score queries
  const configZoneId = useGlobalStore((s) => s.config.zoneId);
  const serverZoneId = useGlobalStore((s) => s.zoneId);
  const effectiveZoneId = configZoneId ?? serverZoneId ?? undefined;

  const manifests = useAccessStore((s) => s.manifests);
  const selectedManifestIndex = useAccessStore((s) => s.selectedManifestIndex);
  const manifestsLoading = useAccessStore((s) => s.manifestsLoading);
  const lastPermissionCheck = useAccessStore((s) => s.lastPermissionCheck);
  const permissionCheckLoading = useAccessStore((s) => s.permissionCheckLoading);
  const alerts = useAccessStore((s) => s.alerts);
  const alertsLoading = useAccessStore((s) => s.alertsLoading);
  const selectedAlertIndex = useAccessStore((s) => s.selectedAlertIndex);
  const leaderboard = useAccessStore((s) => s.leaderboard);
  const leaderboardLoading = useAccessStore((s) => s.leaderboardLoading);
  const credentials = useAccessStore((s) => s.credentials);
  const credentialsLoading = useAccessStore((s) => s.credentialsLoading);
  const disputes = useAccessStore((s) => s.disputes);
  const disputesLoading = useAccessStore((s) => s.disputesLoading);
  const selectedDisputeIndex = useAccessStore((s) => s.selectedDisputeIndex);
  const fraudScores = useAccessStore((s) => s.fraudScores);
  const fraudScoresLoading = useAccessStore((s) => s.fraudScoresLoading);
  const selectedFraudIndex = useAccessStore((s) => s.selectedFraudIndex);
  const activeTab = useAccessStore((s) => s.activeTab);
  const error = useAccessStore((s) => s.error);

  const fetchManifests = useAccessStore((s) => s.fetchManifests);
  const fetchManifestDetail = useAccessStore((s) => s.fetchManifestDetail);
  const fetchAlerts = useAccessStore((s) => s.fetchAlerts);
  const resolveAlert = useAccessStore((s) => s.resolveAlert);
  const fetchLeaderboard = useAccessStore((s) => s.fetchLeaderboard);
  const fetchCredentials = useAccessStore((s) => s.fetchCredentials);
  const fetchDispute = useAccessStore((s) => s.fetchDispute);
  const resolveDispute = useAccessStore((s) => s.resolveDispute);
  const fetchFraudScores = useAccessStore((s) => s.fetchFraudScores);
  const computeFraudScores = useAccessStore((s) => s.computeFraudScores);
  const setActiveTab = useAccessStore((s) => s.setActiveTab);
  const setSelectedManifestIndex = useAccessStore((s) => s.setSelectedManifestIndex);
  const setSelectedAlertIndex = useAccessStore((s) => s.setSelectedAlertIndex);
  const setSelectedDisputeIndex = useAccessStore((s) => s.setSelectedDisputeIndex);
  const setSelectedFraudIndex = useAccessStore((s) => s.setSelectedFraudIndex);

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
      const selected = manifests[selectedManifestIndex];
      if (selected) {
        fetchCredentials(selected.agent_id, client);
      }
    } else if (activeTab === "disputes") {
      for (const d of disputes) {
        fetchDispute(d.id, client);
      }
    } else if (activeTab === "fraud") {
      fetchFraudScores(effectiveZoneId, client);
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
        setSelectedManifestIndex(Math.min(selectedManifestIndex + 1, manifests.length - 1));
      } else if (activeTab === "alerts") {
        setSelectedAlertIndex(Math.min(selectedAlertIndex + 1, alerts.length - 1));
      } else if (activeTab === "disputes") {
        setSelectedDisputeIndex(Math.min(selectedDisputeIndex + 1, disputes.length - 1));
      } else if (activeTab === "fraud") {
        setSelectedFraudIndex(Math.min(selectedFraudIndex + 1, fraudScores.length - 1));
      }
    },
    down: () => {
      if (activeTab === "manifests") {
        setSelectedManifestIndex(Math.min(selectedManifestIndex + 1, manifests.length - 1));
      } else if (activeTab === "alerts") {
        setSelectedAlertIndex(Math.min(selectedAlertIndex + 1, alerts.length - 1));
      } else if (activeTab === "disputes") {
        setSelectedDisputeIndex(Math.min(selectedDisputeIndex + 1, disputes.length - 1));
      } else if (activeTab === "fraud") {
        setSelectedFraudIndex(Math.min(selectedFraudIndex + 1, fraudScores.length - 1));
      }
    },
    k: () => {
      if (activeTab === "manifests") {
        setSelectedManifestIndex(Math.max(selectedManifestIndex - 1, 0));
      } else if (activeTab === "alerts") {
        setSelectedAlertIndex(Math.max(selectedAlertIndex - 1, 0));
      } else if (activeTab === "disputes") {
        setSelectedDisputeIndex(Math.max(selectedDisputeIndex - 1, 0));
      } else if (activeTab === "fraud") {
        setSelectedFraudIndex(Math.max(selectedFraudIndex - 1, 0));
      }
    },
    up: () => {
      if (activeTab === "manifests") {
        setSelectedManifestIndex(Math.max(selectedManifestIndex - 1, 0));
      } else if (activeTab === "alerts") {
        setSelectedAlertIndex(Math.max(selectedAlertIndex - 1, 0));
      } else if (activeTab === "disputes") {
        setSelectedDisputeIndex(Math.max(selectedDisputeIndex - 1, 0));
      } else if (activeTab === "fraud") {
        setSelectedFraudIndex(Math.max(selectedFraudIndex - 1, 0));
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
    return: () => {
      // Manifests: fetch detail to load tuple entries
      if (activeTab === "manifests" && client) {
        const selected = manifests[selectedManifestIndex];
        if (selected) {
          fetchManifestDetail(selected.manifest_id, client);
        }
      }
    },
    r: () => refreshCurrentView(),
    p: () => {
      if (overlay === "none") {
        setOverlay("permissionChecker");
      }
    },
    f: () => {
      if (activeTab === "disputes" && overlay === "none") {
        setOverlay("disputeFiler");
      }
    },
    g: () => {
      if (activeTab === "disputes" && overlay === "none") {
        setOverlay("disputeLookup");
      }
    },
    c: () => {
      // Compute fraud scores
      if (activeTab === "fraud" && client) {
        computeFraudScores(effectiveZoneId, client);
      }
    },
    "shift+r": () => {
      if (!client || overlay !== "none") return;
      if (activeTab === "disputes") {
        const selected = disputes[selectedDisputeIndex];
        if (selected && selected.status !== "resolved" && selected.status !== "dismissed") {
          resolveDispute(selected.id, "Resolved via TUI", client);
        }
      } else if (activeTab === "alerts") {
        const selected = alerts[selectedAlertIndex];
        if (selected && !selected.resolved) {
          resolveAlert(selected.alert_id, "tui-operator", client);
        }
      }
    },
  });

  // Derive the initial manifest ID from the selected manifest
  const selectedManifest = manifests[selectedManifestIndex];
  const initialManifestId = selectedManifest?.manifest_id ?? "";

  const closeOverlay = (): void => setOverlay("none");

  const overlayLabel =
    overlay === "permissionChecker"
      ? " | Permission Checker"
      : overlay === "disputeFiler"
        ? " | File Dispute"
        : overlay === "disputeLookup"
          ? " | Lookup Dispute"
          : "";

  if (overlay !== "none") {
    return (
      <box height="100%" width="100%" flexDirection="column">
        <box height={1} width="100%">
          <text>
            {TAB_ORDER.map((tab) => {
              const label = TAB_LABELS[tab];
              return tab === activeTab ? `[${label}]` : ` ${label} `;
            }).join(" ")}
            {overlayLabel}
          </text>
        </box>
        <box flexGrow={1} borderStyle="single">
          {overlay === "permissionChecker" && (
            <PermissionChecker
              initialManifestId={initialManifestId}
              lastResult={lastPermissionCheck}
              loading={permissionCheckLoading}
              onClose={closeOverlay}
            />
          )}
          {overlay === "disputeFiler" && (
            <DisputeFiler onClose={closeOverlay} />
          )}
          {overlay === "disputeLookup" && (
            <DisputeLookup onClose={closeOverlay} />
          )}
        </box>
      </box>
    );
  }

  // Tab-specific help text
  const HELP: Readonly<Record<AccessTab, string>> = {
    manifests: "j/k:navigate  Enter:show entries  p:perm check  Tab:tab  r:refresh  q:quit",
    alerts: "j/k:navigate  Shift+R:resolve  Tab:tab  r:refresh  q:quit",
    reputation: "Tab:tab  r:refresh  q:quit",
    credentials: "Tab:tab  r:refresh  q:quit",
    disputes: "j/k:navigate  f:file  g:lookup  Shift+R:resolve  Tab:tab  r:refresh  q:quit",
    fraud: "j/k:navigate  c:compute  Tab:tab  r:refresh  q:quit",
  };

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
            selectedIndex={selectedAlertIndex}
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
        {activeTab === "fraud" && (
          <FraudScoreView
            scores={fraudScores}
            selectedIndex={selectedFraudIndex}
            loading={fraudScoresLoading}
          />
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>{HELP[activeTab]}</text>
      </box>
    </box>
  );
}
