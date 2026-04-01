/**
 * Access Control panel: tabbed layout for manifests, alerts,
 * credentials, fraud scores, and delegations.
 *
 * Key bindings:
 *   j/k or up/down : navigate within lists
 *   g / Shift+G    : jump to start / end of list
 *   Tab            : cycle tabs
 *   Escape         : close overlay
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
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useUiStore } from "../../stores/ui-store.js";
import { useVisibleTabs } from "../../shared/hooks/use-visible-tabs.js";
import { SubTabBar } from "../../shared/components/sub-tab-bar.js";
import { subTabCycleBindings, subTabForward } from "../../shared/components/sub-tab-bar-utils.js";
import { useTabFallback } from "../../shared/hooks/use-tab-fallback.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { statusColor } from "../../shared/theme.js";
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
import { ConstraintList } from "./constraint-list.js";
import { ConstraintCreator } from "./constraint-creator.js";
import { ACCESS_TABS } from "../../shared/navigation.js";
type OverlayMode =
  | "none"
  | "permissionChecker"
  | "delegationCreator"
  | "delegationCompleter"
  | "delegationChainView"
  | "namespaceConfigView"
  | "manifestCreator"
  | "constraintCreator";

export default function AccessPanel(): React.ReactNode {
  const client = useApi();
  const confirm = useConfirmStore((s) => s.confirm);
  const overlayActive = useUiStore((s) => s.overlayActive);
  const visibleTabs = useVisibleTabs(ACCESS_TABS);
  const { copy, copied } = useCopy();
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
  const collusionRings = useAccessStore((s) => s.collusionRings);
  const collusionLoading = useAccessStore((s) => s.collusionLoading);
  const fetchCollusionRings = useAccessStore((s) => s.fetchCollusionRings);
  const suspendAgent = useAccessStore((s) => s.suspendAgent);
  const constraints = useAccessStore((s) => s.constraints);
  const constraintsLoading = useAccessStore((s) => s.constraintsLoading);
  const selectedConstraintIndex = useAccessStore((s) => s.selectedConstraintIndex);
  const fetchConstraints = useAccessStore((s) => s.fetchConstraints);
  const deleteConstraint = useAccessStore((s) => s.deleteConstraint);
  const setSelectedConstraintIndex = useAccessStore((s) => s.setSelectedConstraintIndex);
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

  // Clamp selectedCredentialIndex when credentials list shrinks (e.g. after revoke)
  useEffect(() => {
    if (credentials.length > 0 && selectedCredentialIndex >= credentials.length) {
      setSelectedCredentialIndex(Math.max(0, credentials.length - 1));
    }
  }, [credentials.length, selectedCredentialIndex]);

  // Fraud tab: which list is focused (scores vs constraints)
  const [fraudFocus, setFraudFocus] = useState<"scores" | "constraints">("scores");

  // Delegation status filter
  const [delegationFilter, setDelegationFilter] = useState<string | null>(null);

  useTabFallback(visibleTabs, activeTab, setActiveTab);

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
      if (effectiveZoneId) fetchConstraints(effectiveZoneId, client);
    } else if (activeTab === "delegations") {
      fetchDelegations(client, delegationFilter);
    }
  }, [client, activeTab, manifests, selectedManifestIndex, effectiveZoneId, delegationFilter, fetchManifests, fetchAlerts, fetchCredentials, fetchFraudScores, fetchDelegations]);

  // Auto-fetch when tab changes
  useEffect(() => {
    refreshCurrentView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, client]);

  // Shared list navigation (j/k/up/down/g/G) — switches per active tab
  const listNav = listNavigationBindings({
    getIndex: () => {
      if (activeTab === "manifests") return selectedManifestIndex;
      if (activeTab === "alerts") return selectedAlertIndex;
      if (activeTab === "credentials") return selectedCredentialIndex;
      if (activeTab === "fraud") return fraudFocus === "scores" ? selectedFraudIndex : selectedConstraintIndex;
      if (activeTab === "delegations") return selectedDelegationIndex;
      return 0;
    },
    setIndex: (i) => {
      if (activeTab === "manifests") setSelectedManifestIndex(i);
      else if (activeTab === "alerts") setSelectedAlertIndex(i);
      else if (activeTab === "credentials") setSelectedCredentialIndex(i);
      else if (activeTab === "fraud") {
        if (fraudFocus === "scores") setSelectedFraudIndex(i);
        else setSelectedConstraintIndex(i);
      } else if (activeTab === "delegations") setSelectedDelegationIndex(i);
    },
    getLength: () => {
      if (activeTab === "manifests") return manifests.length;
      if (activeTab === "alerts") return alerts.length;
      if (activeTab === "credentials") return credentials.length;
      if (activeTab === "fraud") return fraudFocus === "scores" ? fraudScores.length : constraints.length;
      if (activeTab === "delegations") return delegations.length;
      return 0;
    },
  });

  useKeyboard(overlayActive ? {} : {
    ...listNav,
    ...subTabCycleBindings(visibleTabs, activeTab, setActiveTab),
    escape: () => {
      if (overlay !== "none") {
        setOverlay("none");
      }
    },
    tab: () => {
      if (activeTab === "fraud") {
        setFraudFocus((f) => f === "scores" ? "constraints" : "scores");
        return;
      }
      subTabForward(visibleTabs, activeTab, setActiveTab);
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
      } else if (activeTab === "fraud" && overlay === "none") {
        setOverlay("constraintCreator");
      }
    },
    d: async () => {
      if (activeTab === "fraud" && overlay === "none" && client) {
        const selected = constraints[selectedConstraintIndex];
        if (selected) {
          const ok = await confirm("Delete constraint?", `Delete governance constraint from ${selected.from_agent_id} to ${selected.to_agent_id} [${selected.constraint_type}].`);
          if (!ok) return;
          deleteConstraint(selected.id, client);
        }
      }
    },
    x: async () => {
      if (activeTab === "delegations" && overlay === "none" && client) {
        const selected = delegations[selectedDelegationIndex];
        if (selected && selected.status === "active") {
          revokeDelegation(selected.delegation_id, client);
        }
      } else if (activeTab === "credentials" && overlay === "none" && client) {
        const selected = credentials[selectedCredentialIndex];
        if (selected && selected.is_active) {
          const ok = await confirm("Revoke credential?", "Revoke this credential. The holder will lose access.");
          if (!ok) return;
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
    "shift+x": async () => {
      if (activeTab === "manifests" && overlay === "none" && client) {
        const selected = manifests[selectedManifestIndex];
        if (selected && selected.status === "active") {
          const ok = await confirm("Revoke manifest?", "Revoke this access manifest. Active sessions may be terminated.");
          if (!ok) return;
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
    y: () => {
      if (activeTab === "manifests") {
        const selected = manifests[selectedManifestIndex];
        if (selected) copy(selected.manifest_id);
      } else if (activeTab === "delegations") {
        const selected = delegations[selectedDelegationIndex];
        if (selected) copy(selected.delegation_id);
      }
    },
    "shift+c": () => {
      // Fetch collusion rings (fraud tab)
      if (activeTab === "fraud" && client) {
        fetchCollusionRings(effectiveZoneId, client);
      }
    },
    s: async () => {
      // Suspend selected agent (fraud tab — selected by fraud score index)
      if (activeTab === "fraud" && client) {
        const selected = fraudScores[selectedFraudIndex];
        if (selected) {
          const ok = await confirm("Suspend agent?", "Suspend this agent. It will be unable to act until unsuspended.");
          if (!ok) return;
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
    constraintCreator: " | New Constraint",
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
          {overlay === "constraintCreator" && (
            <ConstraintCreator
              zoneId={effectiveZoneId ?? ""}
              onClose={closeOverlay}
            />
          )}
        </box>
      </box>
    );
  }

  // Tab-specific help text
  const delegationFilterLabel = delegationFilter ? ` [${delegationFilter}]` : "";
  const HELP: Readonly<Record<AccessTab, string>> = {
    manifests: "j/k:navigate  g/G:jump  Enter:show entries  c:new manifest  Shift+X:revoke  p:perm check  y:copy  Esc:close  Tab:tab  r:refresh  q:quit",
    alerts: "j/k:navigate  g/G:jump  Shift+R:resolve  Esc:close  Tab:tab  r:refresh  q:quit",
    credentials: "j/k:navigate  g/G:jump  i:issue  x:revoke  Esc:close  Tab:tab  r:refresh  q:quit",
    fraud: "j/k:navigate  g/G:jump  c:compute  Shift+C:collusion  s:suspend  n:new constraint  d:delete  Tab:focus  Shift+Tab:tab  Esc:close  r:refresh  q:quit",
    delegations: `j/k:navigate  g/G:jump  n:new  x:revoke  o:complete  v:chain  w:namespace  y:copy  f:filter${delegationFilterLabel}  Esc:close  Tab:tab  r:refresh  q:quit`,
  };

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Tab bar */}
      <SubTabBar tabs={visibleTabs} activeTab={activeTab} />

      {/* Permission evaluation result */}
      {lastPermissionCheck && (
        <box height={3} width="100%" borderStyle="single" borderColor={lastPermissionCheck.permission === "allow" ? statusColor.healthy : statusColor.error}>
          <text foregroundColor={lastPermissionCheck.permission === "allow" ? statusColor.healthy : statusColor.error}>
            {`  ${lastPermissionCheck.permission === "allow" ? "[ALLOW]" : "[DENY] "} tool=${lastPermissionCheck.tool_name}  agent=${lastPermissionCheck.agent_id}  manifest=${lastPermissionCheck.manifest_id}`}
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
          <box height="100%" width="100%" flexDirection="column">
            <box flexGrow={1} width="100%">
              <FraudScoreView
                scores={fraudScores}
                selectedIndex={selectedFraudIndex}
                loading={fraudScoresLoading}
              />
            </box>
            <box flexDirection="column" width="100%">
              <box height={1} width="100%">
                <text>{"--- Collusion Rings ---"}</text>
              </box>
              {collusionLoading ? (
                <box height={1} width="100%">
                  <text>Loading collusion rings...</text>
                </box>
              ) : (collusionRings as { confidence: number; members: string[]; ring_type?: string }[]).length === 0 ? (
                <box height={1} width="100%">
                  <text dimColor>No collusion rings detected</text>
                </box>
              ) : (
                (collusionRings as { confidence: number; members: string[]; ring_type?: string }[]).map((ring, i) => {
                  const conf = ring.confidence;
                  const confColor = conf > 0.7 ? statusColor.error : conf >= 0.4 ? statusColor.warning : undefined;
                  const confStr = conf.toFixed(3);
                  const members = ring.members.join(", ");
                  const ringType = ring.ring_type ?? "unknown";
                  return (
                    <box key={`ring-${i}`} height={1} width="100%">
                      <text>
                        {"  "}
                        <span foregroundColor={confColor} dimColor={conf < 0.4}>{confStr}</span>
                        {`  [${ringType}]  ${members}`}
                      </text>
                    </box>
                  );
                })
              )}
            </box>
            <box flexDirection="column" width="100%">
              <box height={1} width="100%">
                <text>{"--- Governance Constraints ---"}</text>
              </box>
              <ConstraintList
                constraints={constraints}
                selectedIndex={selectedConstraintIndex}
                loading={constraintsLoading}
              />
            </box>
          </box>
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
        {copied
          ? <text foregroundColor={statusColor.healthy}>Copied!</text>
          : <text>{HELP[activeTab]}</text>}
      </box>
    </box>
  );
}
