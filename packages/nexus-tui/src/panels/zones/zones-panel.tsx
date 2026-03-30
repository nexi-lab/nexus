/**
 * Zones panel: tabbed layout with Zones list, Bricks health, and Drift report.
 *
 * Keybindings are context-aware — only actions valid for the selected brick's
 * current state are active and displayed in the help bar.
 */

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useZonesStore } from "../../stores/zones-store.js";
import type { ZoneTab } from "../../stores/zones-store.js";
import { useWorkspaceStore } from "../../stores/workspace-store.js";
import { useMcpStore } from "../../stores/mcp-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { jumpToStart, jumpToEnd } from "../../shared/hooks/use-list-navigation.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { useVisibleTabs, type TabDef } from "../../shared/hooks/use-visible-tabs.js";
import { SubTabBar } from "../../shared/components/sub-tab-bar.js";
import { subTabCycleBindings } from "../../shared/components/sub-tab-bar-utils.js";
import { useTabFallback } from "../../shared/hooks/use-tab-fallback.js";
import { ZoneList } from "./zone-list.js";
import { BrickList } from "./brick-list.js";
import { BrickDetail } from "./brick-detail.js";
import { DriftView } from "./drift-view.js";
import { ReindexStatus } from "./reindex-status.js";
import { WorkspacesTab } from "./workspaces-tab.js";
import { McpMountsTab } from "./mcp-mounts-tab.js";
import { CacheTab } from "./cache-tab.js";
import { ConfirmDialog } from "../../shared/components/confirm-dialog.js";
import { allowedActionsForState } from "../../shared/brick-states.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { useUiStore } from "../../stores/ui-store.js";
import { focusColor } from "../../shared/theme.js";

const ALL_TABS: readonly TabDef<ZoneTab>[] = [
  { id: "zones", label: "Zones", brick: null },
  { id: "bricks", label: "Bricks", brick: null },
  { id: "drift", label: "Drift", brick: null },
  { id: "reindex", label: "Reindex", brick: ["search", "versioning"] },
  { id: "workspaces", label: "Workspaces", brick: "workspace" },
  { id: "mcp", label: "MCP", brick: "mcp" },
  { id: "cache", label: "Cache", brick: "cache" },
];
export default function ZonesPanel(): React.ReactNode {
  const client = useApi();
  const visibleTabs = useVisibleTabs(ALL_TABS);

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
  const cacheStats = useZonesStore((s) => s.cacheStats);
  const cacheStatsLoading = useZonesStore((s) => s.cacheStatsLoading);
  const hotFiles = useZonesStore((s) => s.hotFiles);
  const hotFilesLoading = useZonesStore((s) => s.hotFilesLoading);
  const fetchCacheStats = useZonesStore((s) => s.fetchCacheStats);
  const fetchHotFiles = useZonesStore((s) => s.fetchHotFiles);
  const warmupCache = useZonesStore((s) => s.warmupCache);
  const setSelectedIndex = useZonesStore((s) => s.setSelectedIndex);
  const setActiveTab = useZonesStore((s) => s.setActiveTab);

  // Workspace store selectors
  const workspaces = useWorkspaceStore((s) => s.workspaces);
  const workspacesLoading = useWorkspaceStore((s) => s.workspacesLoading);
  const selectedWorkspaceIndex = useWorkspaceStore((s) => s.selectedWorkspaceIndex);
  const fetchWorkspaces = useWorkspaceStore((s) => s.fetchWorkspaces);
  const unregisterWorkspace = useWorkspaceStore((s) => s.unregisterWorkspace);
  const setSelectedWorkspaceIndex = useWorkspaceStore((s) => s.setSelectedWorkspaceIndex);
  const registerWorkspace = useWorkspaceStore((s) => s.registerWorkspace);

  // MCP store selectors
  const mcpMounts = useMcpStore((s) => s.mounts);
  const mcpMountsLoading = useMcpStore((s) => s.mountsLoading);
  const selectedMountIndex = useMcpStore((s) => s.selectedMountIndex);
  const fetchMcpMounts = useMcpStore((s) => s.fetchMounts);
  const unmountServer = useMcpStore((s) => s.unmountServer);
  const syncServer = useMcpStore((s) => s.syncServer);
  const fetchTools = useMcpStore((s) => s.fetchTools);
  const mountServer = useMcpStore((s) => s.mountServer);
  const setSelectedMountIndex = useMcpStore((s) => s.setSelectedMountIndex);

  // Focus pane (ui-store)
  const uiFocusPane = useUiStore((s) => s.getFocusPane("zones"));
  const toggleFocus = useUiStore((s) => s.toggleFocusPane);
  const overlayActive = useUiStore((s) => s.overlayActive);

  useTabFallback(visibleTabs, activeTab, setActiveTab);

  // Track in-flight brick operations (mount, unmount, reset, etc.)
  const [operationInProgress, setOperationInProgress] = useState(false);

  // Input mode state for create/register flows (multi-field forms)
  const [inputMode, setInputMode] = useState<"none" | "workspace" | "mcpMount">("none");
  const [inputFields, setInputFields] = useState<Record<string, string>>({});
  const [inputActiveField, setInputActiveField] = useState(0);

  const WS_FIELDS = ["path", "name", "description", "scope", "ttl_seconds"] as const;
  const MCP_FIELDS = ["name", "command_or_url", "description"] as const;

  const currentFields = inputMode === "workspace" ? WS_FIELDS
    : inputMode === "mcpMount" ? MCP_FIELDS : [] as const;
  const currentFieldName = currentFields[inputActiveField] ?? "";

  // Confirmation dialog state for destructive actions
  const [confirmUnregister, setConfirmUnregister] = useState(false);
  const [confirmWorkspaceUnregister, setConfirmWorkspaceUnregister] = useState(false);
  const [confirmMcpUnmount, setConfirmMcpUnmount] = useState(false);

  const anyDialogOpen = confirmUnregister || confirmWorkspaceUnregister || confirmMcpUnmount;

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
    } else if (activeTab === "workspaces") {
      fetchWorkspaces(client);
    } else if (activeTab === "mcp") {
      fetchMcpMounts(client);
    } else if (activeTab === "cache") {
      fetchCacheStats(client);
      fetchHotFiles(client);
    }
  }, [activeTab, client, fetchZones, fetchBricks, fetchDrift, fetchWorkspaces, fetchMcpMounts, fetchCacheStats, fetchHotFiles]);

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

  // Workspace unregister confirmation handlers
  const handleConfirmWorkspaceUnregister = useCallback(() => {
    if (!client) return;
    const ws = workspaces[selectedWorkspaceIndex];
    if (ws) {
      unregisterWorkspace(ws.path, client);
    }
    setConfirmWorkspaceUnregister(false);
  }, [client, workspaces, selectedWorkspaceIndex, unregisterWorkspace]);

  const handleCancelWorkspaceUnregister = useCallback(() => {
    setConfirmWorkspaceUnregister(false);
  }, []);

  // MCP unmount confirmation handlers
  const handleConfirmMcpUnmount = useCallback(() => {
    if (!client) return;
    const mount = mcpMounts[selectedMountIndex];
    if (mount) {
      unmountServer(mount.name, client);
    }
    setConfirmMcpUnmount(false);
  }, [client, mcpMounts, selectedMountIndex, unmountServer]);

  const handleCancelMcpUnmount = useCallback(() => {
    setConfirmMcpUnmount(false);
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

  // Compute current list length and set-index for navigation across all tabs
  const currentListLength = useCallback((): number => {
    if (activeTab === "zones") return zones.length;
    if (activeTab === "bricks") return bricks.length;
    if (activeTab === "workspaces") return workspaces.length;
    if (activeTab === "mcp") return mcpMounts.length;
    return 0;
  }, [activeTab, zones.length, bricks.length, workspaces.length, mcpMounts.length]);

  const currentNavIndex = useCallback((): number => {
    if (activeTab === "workspaces") return selectedWorkspaceIndex;
    if (activeTab === "mcp") return selectedMountIndex;
    return selectedIndex;
  }, [activeTab, selectedIndex, selectedWorkspaceIndex, selectedMountIndex]);

  const setCurrentNavIndex = useCallback((index: number): void => {
    if (activeTab === "workspaces") {
      setSelectedWorkspaceIndex(index);
    } else if (activeTab === "mcp") {
      setSelectedMountIndex(index);
    } else {
      setSelectedIndex(index);
    }
  }, [activeTab, setSelectedIndex, setSelectedWorkspaceIndex, setSelectedMountIndex]);

  // In input mode, capture printable characters into the active field
  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      if (inputMode === "none") return;
      const field = currentFieldName;
      if (!field) return;
      if (keyName.length === 1) {
        setInputFields((f) => ({ ...f, [field]: (f[field] ?? "") + keyName }));
      } else if (keyName === "space") {
        setInputFields((f) => ({ ...f, [field]: (f[field] ?? "") + " " }));
      }
    },
    [inputMode, currentFieldName],
  );

  useKeyboard(
    overlayActive
      ? {}
      : anyDialogOpen
      ? {} // ConfirmDialog handles its own keys when visible
      : inputMode !== "none"
        ? {
            return: () => {
              if (!client) { setInputMode("none"); return; }
              const f = inputFields;
              if (inputMode === "workspace") {
                const path = (f.path ?? "").trim();
                if (!path) { setInputMode("none"); return; }
                registerWorkspace({
                  path,
                  name: (f.name ?? "").trim() || path.split("/").pop() || path,
                  description: (f.description ?? "").trim() || undefined,
                  scope: (f.scope ?? "").trim() || undefined,
                  ttl_seconds: f.ttl_seconds?.trim() ? parseInt(f.ttl_seconds.trim(), 10) : undefined,
                }, client);
              } else if (inputMode === "mcpMount") {
                const val = (f.command_or_url ?? "").trim();
                const name = (f.name ?? "").trim() || val.split(/[\s/]/).pop() || "mcp-server";
                if (!val) { setInputMode("none"); return; }
                if (val.startsWith("http://") || val.startsWith("https://")) {
                  mountServer({ name, url: val, description: (f.description ?? "").trim() || undefined }, client);
                } else {
                  mountServer({ name, command: val, description: (f.description ?? "").trim() || undefined }, client);
                }
              }
              setInputMode("none");
              setInputFields({});
              setInputActiveField(0);
            },
            escape: () => {
              setInputMode("none");
              setInputFields({});
              setInputActiveField(0);
            },
            backspace: () => {
              const field = currentFieldName;
              if (field) {
                setInputFields((ff) => ({ ...ff, [field]: (ff[field] ?? "").slice(0, -1) }));
              }
            },
            tab: () => {
              setInputActiveField((i) => (i + 1) % currentFields.length);
            },
          }
        : {
            j: () => {
              const maxLen = currentListLength();
              if (maxLen > 0) {
                setCurrentNavIndex(Math.min(currentNavIndex() + 1, maxLen - 1));
              }
            },
            down: () => {
              const maxLen = currentListLength();
              if (maxLen > 0) {
                setCurrentNavIndex(Math.min(currentNavIndex() + 1, maxLen - 1));
              }
            },
            k: () => {
              setCurrentNavIndex(Math.max(currentNavIndex() - 1, 0));
            },
            up: () => {
              setCurrentNavIndex(Math.max(currentNavIndex() - 1, 0));
            },
            ...subTabCycleBindings(visibleTabs, activeTab, setActiveTab),
            "shift+tab": () => toggleFocus("zones"),
            // n: Register workspace or mount MCP server
            n: () => {
              if (activeTab === "workspaces") {
                setInputMode("workspace");
                setInputFields({});
                setInputActiveField(0);
              } else if (activeTab === "mcp") {
                setInputMode("mcpMount");
                setInputFields({});
                setInputActiveField(0);
              }
            },
            // M (shift+m): Mount — valid for registered/unmounted
            "shift+m": () => {
              if (!client || !selectedBrick || !allowed.has("mount")) return;
              setOperationInProgress(true);
              mountBrick(selectedBrick.name, client).finally(() => setOperationInProgress(false));
            },
            // U: Unmount — valid for active
            "shift+u": () => {
              if (!client || !selectedBrick || !allowed.has("unmount")) return;
              setOperationInProgress(true);
              unmountBrick(selectedBrick.name, client).finally(() => setOperationInProgress(false));
            },
            // D: Unregister — valid for unmounted (with confirmation)
            "shift+d": () => {
              if (!client || !selectedBrick || !allowed.has("unregister")) return;
              setConfirmUnregister(true);
            },
            // m: Remount (existing) — valid for unmounted only
            m: () => {
              if (!client || !selectedBrick || !allowed.has("remount")) return;
              setOperationInProgress(true);
              remountBrick(selectedBrick.name, client).finally(() => setOperationInProgress(false));
            },
            // x: Reset (existing) — valid for failed
            x: () => {
              if (!client || !selectedBrick || !allowed.has("reset")) return;
              setOperationInProgress(true);
              resetBrick(selectedBrick.name, client).finally(() => setOperationInProgress(false));
            },
            // d: Unregister workspace or unmount MCP (with confirmation)
            d: () => {
              if (!client) return;
              if (activeTab === "workspaces") {
                const ws = workspaces[selectedWorkspaceIndex];
                if (ws) setConfirmWorkspaceUnregister(true);
              } else if (activeTab === "mcp") {
                const mount = mcpMounts[selectedMountIndex];
                if (mount) setConfirmMcpUnmount(true);
              }
            },
            // s: Sync MCP server
            s: () => {
              if (!client || activeTab !== "mcp") return;
              const mount = mcpMounts[selectedMountIndex];
              if (mount) syncServer(mount.name, client);
            },
            // return: Show tools for selected MCP mount
            return: () => {
              if (!client || activeTab !== "mcp") return;
              const mount = mcpMounts[selectedMountIndex];
              if (mount) fetchTools(mount.name, client);
            },
            w: () => {
              // Warmup cache with hot files
              if (activeTab === "cache" && client && hotFiles.length > 0) {
                const paths = hotFiles.map((f) => String((f as Record<string, unknown>).path ?? "")).filter(Boolean);
                if (paths.length > 0) warmupCache(paths, client);
              }
            },
            r: () => {
              refreshActiveTab();
            },
            g: () => {
              setCurrentNavIndex(jumpToStart());
            },
            "shift+g": () => {
              const len = currentListLength();
              setCurrentNavIndex(jumpToEnd(len));
            },
          },
    !overlayActive && inputMode !== "none" ? handleUnhandledKey : undefined,
  );

  // Context-aware help text per tab
  const helpText = useMemo((): string => {
    const base = "j/k:navigate  Tab:switch tab  r:refresh  q:quit";
    if (activeTab === "bricks") return brickHelpText;
    if (activeTab === "workspaces") return "j/k:navigate  n:register  d:unregister  Tab:tab  r:refresh  q:quit";
    if (activeTab === "mcp") return "j/k:navigate  n:mount  d:unmount  s:sync  Enter:tools  Tab:tab  r:refresh  q:quit";
    if (activeTab === "cache") return "w:warmup hot files  Tab:tab  r:refresh  q:quit";
    return base;
  }, [activeTab, brickHelpText]);

  return (
    <box height="100%" width="100%" flexDirection="column">
      <SubTabBar tabs={visibleTabs} activeTab={activeTab} />

      {/* Multi-field input form for register/mount */}
      {inputMode !== "none" && (
        <box flexDirection="column" width="100%">
          {currentFields.map((field, i) => {
            const isActive = i === inputActiveField;
            const val = inputFields[field] ?? "";
            const hint = field === "scope" ? " (persistent|session)" : field === "ttl_seconds" ? " (seconds, blank=none)" : field === "command_or_url" ? " (URL for SSE, command for stdio)" : "";
            return (
              <box key={field} height={1} width="100%">
                <text>{isActive ? `> ${field}: ${val}\u2588${hint}` : `  ${field}: ${val}`}</text>
              </box>
            );
          })}
          <box height={1} width="100%">
            <text>{"Tab:next field  Enter:submit  Escape:cancel"}</text>
          </box>
        </box>
      )}

      {/* Error display */}
      {error && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {/* Brick operation in-flight indicator */}
      {operationInProgress && (
        <box height={1} width="100%">
          <LoadingIndicator message="Operation in progress..." centered={false} />
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
            <box width="30%" height="100%" borderStyle="single" borderColor={uiFocusPane === "left" ? focusColor.activeBorder : focusColor.inactiveBorder} flexDirection="column">
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
            <box width="70%" height="100%" borderStyle="single" borderColor={uiFocusPane === "right" ? focusColor.activeBorder : focusColor.inactiveBorder}>
              <BrickDetail brick={brickDetail} loading={detailLoading} />
            </box>
          </>
        )}

        {activeTab === "drift" && (
          <DriftView drift={driftReport} loading={driftLoading} />
        )}

        {activeTab === "reindex" && <ReindexStatus />}

        {activeTab === "workspaces" && (
          <WorkspacesTab
            workspaces={workspaces}
            selectedIndex={selectedWorkspaceIndex}
            loading={workspacesLoading}
          />
        )}

        {activeTab === "mcp" && (
          <McpMountsTab
            mounts={mcpMounts}
            selectedIndex={selectedMountIndex}
            loading={mcpMountsLoading}
          />
        )}

        {activeTab === "cache" && (
          <CacheTab
            stats={cacheStats}
            hotFiles={hotFiles}
            loading={cacheStatsLoading || hotFilesLoading}
          />
        )}
      </box>

      {/* Context-aware help bar */}
      <box height={1} width="100%">
        <text>
          {inputMode !== "none"
            ? `${inputMode === "workspace" ? "Register Workspace" : "Mount MCP Server"} — Tab:field  Enter:submit  Escape:cancel`
            : helpText}
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

      {/* Workspace unregister confirmation dialog */}
      <ConfirmDialog
        visible={confirmWorkspaceUnregister}
        title="Unregister Workspace"
        message={`Unregister workspace "${workspaces[selectedWorkspaceIndex]?.name ?? ""}"?`}
        onConfirm={handleConfirmWorkspaceUnregister}
        onCancel={handleCancelWorkspaceUnregister}
      />

      {/* MCP unmount confirmation dialog */}
      <ConfirmDialog
        visible={confirmMcpUnmount}
        title="Unmount MCP Server"
        message={`Unmount MCP server "${mcpMounts[selectedMountIndex]?.name ?? ""}"?`}
        onConfirm={handleConfirmMcpUnmount}
        onCancel={handleCancelMcpUnmount}
      />
    </box>
  );
}
