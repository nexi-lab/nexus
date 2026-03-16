/**
 * Access Control panel: tabbed layout for manifests, alerts,
 * credentials, fraud scores, and delegations.
 *
 * Key bindings:
 *   j/k or up/down : navigate within lists
 *   Tab            : cycle tabs
 *   Enter          : manifests -> fetch detail (tuple entries)
 *   p              : open permission checker (+ governance edge check)
 *   Shift+R        : (alerts tab) resolve selected
 *   c              : (manifests tab) create new manifest; (fraud tab) compute fraud scores
 *   Shift+X        : (manifests tab) revoke selected manifest
 *   n              : (delegations tab) create new delegation
 *   x              : (delegations tab) revoke selected delegation
 *   o              : (delegations tab) complete selected delegation
 *   v              : (delegations tab) view delegation chain
 *   w              : (delegations tab) view namespace config
 *   r              : refresh current tab
 */

import React, { useState, useEffect, useCallback } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import type { AccessTab } from "../../stores/access-store.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useVisibleTabs, type TabDef } from "../../shared/hooks/use-visible-tabs.js";
import { ManifestList } from "./manifest-list.js";
import { AlertList } from "./alert-list.js";
import { CredentialList } from "./credential-list.js";
import { FraudScoreView } from "./fraud-score-view.js";
import { DelegationList } from "./delegation-list.js";
import { PermissionChecker } from "./permission-checker.js";
import { DelegationCreator } from "./delegation-creator.js";
import { DelegationCompleter } from "./delegation-completer.js";
import { DelegationChainView } from "./delegation-chain-view.js";
import { NamespaceConfigView } from "./namespace-config-view.js";
import { ManifestCreator } from "./manifest-creator.js";

const ALL_TABS: readonly TabDef<AccessTab>[] = [
  { id: "manifests", label: "Manifests", brick: "access_manifest" },
  { id: "alerts", label: "Alerts", brick: "governance" },
  { id: "credentials", label: "Credentials", brick: "auth" },
  { id: "fraud", label: "Fraud", brick: "governance" },
  { id: "delegations", label: "Delegations", brick: "delegation" },
];
const TAB_LABELS: Readonly<Record<AccessTab, string>> = {
  manifests: "Manifests",
  alerts: "Alerts",
  credentials: "Credentials",
  fraud: "Fraud",
  delegations: "Delegations",
};

type OverlayMode =
  | "none"
  | "permissionChecker"
  | "delegationCreator"
  | "delegationCompleter"
  | "delegationChainView"
  | "namespaceConfigView"
  | "manifestCreator";

export default function AccessPanel(): React.ReactNode {
  const client = useApi();
  const visibleTabs = useVisibleTabs(ALL_TABS);
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
  const credentials = useAccessStore((s) => s.credentials);
  const credentialsLoading = useAccessStore((s) => s.credentialsLoading);
  const fraudScores = useAccessStore((s) => s.fraudScores);
  const fraudScoresLoading = useAccessStore((s) => s.fraudScoresLoading);
  const selectedFraudIndex = useAccessStore((s) => s.selectedFraudIndex);
  const delegations = useAccessStore((s) => s.delegations);
  const delegationsLoading = useAccessStore((s) => s.delegationsLoading);
  const selectedDelegationIndex = useAccessStore((s) => s.selectedDelegationIndex);
  const governanceCheck = useAccessStore((s) => s.governanceCheck);
  const governanceCheckLoading = useAccessStore((s) => s.governanceCheckLoading);
  const activeTab = useAccessStore((s) => s.activeTab);
  const error = useAccessStore((s) => s.error);

  const fetchManifests = useAccessStore((s) => s.fetchManifests);
  const fetchManifestDetail = useAccessStore((s) => s.fetchManifestDetail);
  const revokeManifest = useAccessStore((s) => s.revokeManifest);
  const fetchAlerts = useAccessStore((s) => s.fetchAlerts);
  const resolveAlert = useAccessStore((s) => s.resolveAlert);
  const fetchCredentials = useAccessStore((s) => s.fetchCredentials);
  const issueCredential = useAccessStore((s) => s.issueCredential);
  const fetchCollusionRings = useAccessStore((s) => s.fetchCollusionRings);
  const suspendAgent = useAccessStore((s) => s.suspendAgent);
  const fraudScores = useAccessStore((s) => s.fraudScores);
  const selectedFraudIndex = useAccessStore((s) => s.selectedFraudIndex);
  const revokeCredential = useAccessStore((s) => s.revokeCredential);
  const fetchFraudScores = useAccessStore((s) => s.fetchFraudScores);
  const computeFraudScores = useAccessStore((s) => s.computeFraudScores);
  const fetchDelegations = useAccessStore((s) => s.fetchDelegations);
  const revokeDelegation = useAccessStore((s) => s.revokeDelegation);
  const setActiveTab = useAccessStore((s) => s.setActiveTab);
  const setSelectedManifestIndex = useAccessStore((s) => s.setSelectedManifestIndex);
  const setSelectedAlertIndex = useAccessStore((s) => s.setSelectedAlertIndex);
  const setSelectedFraudIndex = useAccessStore((s) => s.setSelectedFraudIndex);
  const setSelectedDelegationIndex = useAccessStore((s) => s.setSelectedDelegationIndex);

  // Credential selection index
  const [selectedCredentialIndex, setSelectedCredentialIndex] = useState(0);

  // Delegation status filter
  const [delegationFilter, setDelegationFilter] = useState<string | null>(null);

  // Fall back to first visible tab if the active tab becomes hidden
  const visibleIds = visibleTabs.map((t) => t.id);
  useEffect(() => {
    if (visibleIds.length > 0 && !visibleIds.includes(activeTab)) {
      setActiveTab(visibleIds[0]!);
    }
  }, [visibleIds.join(","), activeTab, setActiveTab]);

  // Refresh current view based on active tab
  const refreshCurrentView = useCallback((): void => {
    if (!client) return;

    if (activeTab === "manifests") {
      fetchManifests(client);
    } else if (activeTab === "alerts") {
      fetchAlerts(effectiveZoneId, client);
    } else if (activeTab === "credentials") {
      const selected = manifests[selectedManifestIndex];
      if (selected) {
        fetchCredentials(selected.agent_id, client);
      }
    } else if (activeTab === "fraud") {
      fetchFraudScores(effectiveZoneId, client);
    } else if (activeTab === "delegations") {
      fetchDelegations(client, delegationFilter);
    }
  }, [client, activeTab, manifests, selectedManifestIndex, effectiveZoneId, delegationFilter, fetchManifests, fetchAlerts, fetchCredentials, fetchFraudScores, fetchDelegations]);

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
      } else if (activeTab === "credentials") {
        setSelectedCredentialIndex(Math.min(selectedCredentialIndex + 1, credentials.length - 1));
      } else if (activeTab === "fraud") {
        setSelectedFraudIndex(Math.min(selectedFraudIndex + 1, fraudScores.length - 1));
      } else if (activeTab === "delegations") {
        setSelectedDelegationIndex(Math.min(selectedDelegationIndex + 1, delegations.length - 1));
      }
    },
    down: () => {
      if (activeTab === "manifests") {
        setSelectedManifestIndex(Math.min(selectedManifestIndex + 1, manifests.length - 1));
      } else if (activeTab === "alerts") {
        setSelectedAlertIndex(Math.min(selectedAlertIndex + 1, alerts.length - 1));
      } else if (activeTab === "credentials") {
        setSelectedCredentialIndex(Math.min(selectedCredentialIndex + 1, credentials.length - 1));
      } else if (activeTab === "fraud") {
        setSelectedFraudIndex(Math.min(selectedFraudIndex + 1, fraudScores.length - 1));
      } else if (activeTab === "delegations") {
        setSelectedDelegationIndex(Math.min(selectedDelegationIndex + 1, delegations.length - 1));
      }
    },
    k: () => {
      if (activeTab === "manifests") {
        setSelectedManifestIndex(Math.max(selectedManifestIndex - 1, 0));
      } else if (activeTab === "alerts") {
        setSelectedAlertIndex(Math.max(selectedAlertIndex - 1, 0));
      } else if (activeTab === "credentials") {
        setSelectedCredentialIndex(Math.max(selectedCredentialIndex - 1, 0));
      } else if (activeTab === "fraud") {
        setSelectedFraudIndex(Math.max(selectedFraudIndex - 1, 0));
      } else if (activeTab === "delegations") {
        setSelectedDelegationIndex(Math.max(selectedDelegationIndex - 1, 0));
      }
    },
    up: () => {
      if (activeTab === "manifests") {
        setSelectedManifestIndex(Math.max(selectedManifestIndex - 1, 0));
      } else if (activeTab === "alerts") {
        setSelectedAlertIndex(Math.max(selectedAlertIndex - 1, 0));
      } else if (activeTab === "credentials") {
        setSelectedCredentialIndex(Math.max(selectedCredentialIndex - 1, 0));
      } else if (activeTab === "fraud") {
        setSelectedFraudIndex(Math.max(selectedFraudIndex - 1, 0));
      } else if (activeTab === "delegations") {
        setSelectedDelegationIndex(Math.max(selectedDelegationIndex - 1, 0));
      }
    },
    tab: () => {
      const ids = visibleTabs.map((t) => t.id);
      const currentIdx = ids.indexOf(activeTab);
      const nextIdx = (currentIdx + 1) % ids.length;
      const nextTab = ids[nextIdx];
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
    n: () => {
      if (activeTab === "delegations" && overlay === "none") {
        setOverlay("delegationCreator");
      }
    },
    x: () => {
      if (activeTab === "delegations" && overlay === "none" && client) {
        const selected = delegations[selectedDelegationIndex];
        if (selected && selected.status === "active") {
          revokeDelegation(selected.delegation_id, client);
        }
      } else if (activeTab === "credentials" && overlay === "none" && client) {
        const selected = credentials[selectedCredentialIndex];
        if (selected && selected.is_active) {
          revokeCredential(selected.credential_id, selected.subject_agent_id, client);
        }
      }
    },
    o: () => {
      if (activeTab === "delegations" && overlay === "none") {
        const selected = delegations[selectedDelegationIndex];
        if (selected && selected.status === "active") {
          setOverlay("delegationCompleter");
        }
      }
    },
    v: () => {
      if (activeTab === "delegations" && overlay === "none") {
        const selected = delegations[selectedDelegationIndex];
        if (selected) {
          setOverlay("delegationChainView");
        }
      }
    },
    w: () => {
      if (activeTab === "delegations" && overlay === "none") {
        const selected = delegations[selectedDelegationIndex];
        if (selected) {
          setOverlay("namespaceConfigView");
        }
      }
    },
    c: () => {
      if (activeTab === "manifests" && overlay === "none") {
        setOverlay("manifestCreator");
      } else if (activeTab === "fraud" && client) {
        // Compute fraud scores
        computeFraudScores(effectiveZoneId, client);
      }
    },
    "shift+x": () => {
      if (activeTab === "manifests" && overlay === "none" && client) {
        const selected = manifests[selectedManifestIndex];
        if (selected && selected.status === "active") {
          revokeManifest(selected.manifest_id, client);
        }
      }
    },
    "shift+r": () => {
      if (!client || overlay !== "none") return;
      if (activeTab === "alerts") {
        const selected = alerts[selectedAlertIndex];
        if (selected && !selected.resolved) {
          resolveAlert(selected.alert_id, "tui-operator", effectiveZoneId, client);
        }
      }
    },
    f: () => {
      if (activeTab === "delegations") {
        const cycle: (string | null)[] = [null, "active", "revoked", "expired", "completed"];
        const idx = cycle.indexOf(delegationFilter);
        const next = cycle[(idx + 1) % cycle.length] ?? null;
        setDelegationFilter(next);
        if (client) fetchDelegations(client, next);
      }
    },
    i: () => {
      // Issue credential for the selected agent (from manifests tab's agent_id)
      if (activeTab === "credentials" && client) {
        const manifest = manifests[selectedManifestIndex];
        if (manifest) {
          issueCredential(manifest.agent_id, {}, client);
        }
      }
    },
    g: () => {
      // Fetch collusion rings (fraud tab)
      if (activeTab === "fraud" && client) {
        fetchCollusionRings(effectiveZoneId, client);
      }
    },
    s: () => {
      // Suspend selected agent (fraud tab — selected by fraud score index)
      if (activeTab === "fraud" && client) {
        const selected = fraudScores[selectedFraudIndex];
        if (selected) {
          suspendAgent(selected.agent_id, "Suspended via TUI", effectiveZoneId, client);
        }
      }
    },
  });

  // Derive selected items for overlays
  const selectedManifest = manifests[selectedManifestIndex];
  const initialManifestId = selectedManifest?.manifest_id ?? "";
  const selectedDelegation = delegations[selectedDelegationIndex];

  const closeOverlay = (): void => setOverlay("none");

  const OVERLAY_LABELS: Readonly<Record<OverlayMode, string>> = {
    none: "",
    permissionChecker: " | Permission Checker",
    delegationCreator: " | New Delegation",
    delegationCompleter: " | Complete Delegation",
    delegationChainView: " | Delegation Chain",
    namespaceConfigView: " | Namespace Editor",
    manifestCreator: " | New Manifest",
  };
  const overlayLabel = OVERLAY_LABELS[overlay];

  if (overlay !== "none") {
    return (
      <box height="100%" width="100%" flexDirection="column">
        <box height={1} width="100%">
          <text>
            {visibleTabs.map((tab) => {
              return tab.id === activeTab ? `[${tab.label}]` : ` ${tab.label} `;
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
              governanceCheck={governanceCheck}
              governanceCheckLoading={governanceCheckLoading}
              zoneId={effectiveZoneId}
              onClose={closeOverlay}
            />
          )}
          {overlay === "delegationCreator" && (
            <DelegationCreator onClose={closeOverlay} />
          )}
          {overlay === "delegationCompleter" && (
            <DelegationCompleter
              delegationId={selectedDelegation?.delegation_id ?? ""}
              onClose={closeOverlay}
            />
          )}
          {overlay === "delegationChainView" && (
            <DelegationChainView
              delegationId={selectedDelegation?.delegation_id ?? ""}
              onClose={closeOverlay}
            />
          )}
          {overlay === "namespaceConfigView" && (
            <NamespaceConfigView
              delegationId={selectedDelegation?.delegation_id ?? ""}
              onClose={closeOverlay}
            />
          )}
          {overlay === "manifestCreator" && (
            <ManifestCreator onClose={closeOverlay} />
          )}
        </box>
      </box>
    );
  }

  // Tab-specific help text
  const delegationFilterLabel = delegationFilter ? ` [${delegationFilter}]` : "";
  const HELP: Readonly<Record<AccessTab, string>> = {
    manifests: "j/k:navigate  Enter:show entries  c:new manifest  Shift+X:revoke  p:perm check  Tab:tab  r:refresh  q:quit",
    alerts: "j/k:navigate  Shift+R:resolve  Tab:tab  r:refresh  q:quit",
    credentials: "j/k:navigate  i:issue  x:revoke  Tab:tab  r:refresh  q:quit",
    fraud: "j/k:navigate  c:compute  g:collusion  s:suspend agent  Tab:tab  r:refresh  q:quit",
    delegations: `j/k:navigate  n:new  x:revoke  o:complete  v:chain  w:namespace  f:filter${delegationFilterLabel}  Tab:tab  r:refresh  q:quit`,
  };

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Tab bar */}
      <box height={1} width="100%">
        <text>
          {visibleTabs.map((tab) => {
            return tab.id === activeTab ? `[${tab.label}]` : ` ${tab.label} `;
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
        {activeTab === "credentials" && (
          <CredentialList
            credentials={credentials}
            loading={credentialsLoading}
          />
        )}
        {activeTab === "fraud" && (
          <FraudScoreView
            scores={fraudScores}
            selectedIndex={selectedFraudIndex}
            loading={fraudScoresLoading}
          />
        )}
        {activeTab === "delegations" && (
          <DelegationList
            delegations={delegations}
            selectedIndex={selectedDelegationIndex}
            loading={delegationsLoading}
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
