/**
 * Mounted tab: lists mounted connectors with sync control.
 *
 * Supports: mount list navigation, sync trigger, unmount, sync status display.
 */

import React, { useEffect, useCallback } from "react";
import type { FetchClient } from "@nexus-ai-fs/api-client";
import { useConnectorsStore } from "../../stores/connectors-store.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { statusColor } from "../../shared/theme.js";

interface MountedTabProps {
  readonly client: FetchClient;
  readonly overlayActive: boolean;
}

export function MountedTab({ client, overlayActive }: MountedTabProps): React.ReactNode {
  const mounts = useConnectorsStore((s) => s.mounts);
  const loading = useConnectorsStore((s) => s.mountsLoading);
  const selectedIndex = useConnectorsStore((s) => s.selectedMountIndex);
  const syncingMounts = useConnectorsStore((s) => s.syncingMounts);
  const lastSyncResult = useConnectorsStore((s) => s.lastSyncResult);

  const setSelectedIndex = useConnectorsStore((s) => s.setSelectedMountIndex);
  const fetchMounts = useConnectorsStore((s) => s.fetchMounts);
  const triggerSync = useConnectorsStore((s) => s.triggerSync);
  const unmountConnector = useConnectorsStore((s) => s.unmountConnector);
  const clearSyncResult = useConnectorsStore((s) => s.clearSyncResult);

  const confirm = useConfirmStore((s) => s.confirm);

  // Auto-fetch on mount
  useEffect(() => {
    if (mounts.length === 0) {
      fetchMounts(client);
    }
  }, [client, mounts.length, fetchMounts]);

  const handleSync = useCallback(() => {
    const selected = mounts[selectedIndex];
    if (selected) {
      triggerSync(selected.mount_point, client);
    }
  }, [mounts, selectedIndex, triggerSync, client]);

  const handleUnmount = useCallback(async () => {
    const selected = mounts[selectedIndex];
    if (!selected) return;
    const ok = await confirm(
      "Unmount connector?",
      `Unmount ${selected.mount_point}. Synced data will remain in the VFS.`,
    );
    if (!ok) return;
    unmountConnector(selected.mount_point, client);
  }, [mounts, selectedIndex, unmountConnector, client, confirm]);

  const listNav = listNavigationBindings({
    getIndex: () => selectedIndex,
    setIndex: setSelectedIndex,
    getLength: () => mounts.length,
  });

  useKeyboard(
    overlayActive
      ? {}
      : {
          ...listNav,
          s: handleSync,
          u: handleUnmount,
          r: () => fetchMounts(client),
        },
  );

  if (loading && mounts.length === 0) {
    return <LoadingIndicator message="Loading mounts..." />;
  }

  const selectedMount = mounts[selectedIndex];

  return (
    <box flexDirection="column" height="100%" width="100%">
      {/* Sync result banner */}
      {lastSyncResult && (
        <box height={2} width="100%" borderStyle="single" marginBottom={1}>
          {lastSyncResult.error ? (
            <text foregroundColor={statusColor.error}>{`Sync error: ${lastSyncResult.error}`}</text>
          ) : (
            <text foregroundColor={statusColor.healthy}>
              {`Synced ${lastSyncResult.files_synced} files`}
              {lastSyncResult.is_delta ? ` (delta: +${lastSyncResult.delta_added} -${lastSyncResult.delta_deleted})` : ""}
            </text>
          )}
        </box>
      )}

      {/* Mount list */}
      <box flexGrow={1} flexDirection="column">
        {mounts.length === 0 ? (
          <box height={1} width="100%">
            <text foregroundColor={statusColor.dim}>No connectors mounted. Go to Available tab to mount one.</text>
          </box>
        ) : (
          mounts.map((m, i) => {
            const isSyncing = syncingMounts.has(m.mount_point);
            const selected = i === selectedIndex;
            const prefix = selected ? "▶ " : "  ";

            return (
              <box key={m.mount_point} height={1} width="100%">
                <text>
                  <span foregroundColor={selected ? statusColor.info : undefined}>{prefix}</span>
                  <span bold={selected} foregroundColor={statusColor.reference}>{m.mount_point}</span>
                  <span foregroundColor={statusColor.dim}>{m.readonly ? " (ro)" : ""}</span>
                  {m.skill_name && (
                    <span foregroundColor={statusColor.dim}>{`  skill:${m.skill_name}`}</span>
                  )}
                  {m.operations.length > 0 && (
                    <span foregroundColor={statusColor.dim}>{`  ops:${m.operations.length}`}</span>
                  )}
                  {isSyncing && (
                    <span foregroundColor={statusColor.warning}>{`  [syncing…]`}</span>
                  )}
                  {m.sync_status && !isSyncing && (
                    <span foregroundColor={m.sync_status === "error" ? statusColor.error : statusColor.healthy}>
                      {`  [${m.sync_status}]`}
                    </span>
                  )}
                  {m.last_sync && (
                    <span foregroundColor={statusColor.dim}>{`  last:${m.last_sync}`}</span>
                  )}
                </text>
              </box>
            );
          })
        )}
      </box>

      {/* Details for selected mount */}
      {selectedMount && (
        <box height={3} width="100%" borderStyle="single" marginTop={1}>
          <box flexDirection="column" width="100%">
            <box height={1} width="100%">
              <text>
                <span bold>{selectedMount.mount_point}</span>
                <span foregroundColor={statusColor.dim}>
                  {selectedMount.readonly ? "  read-only" : "  read-write"}
                </span>
              </text>
            </box>
            {selectedMount.operations.length > 0 && (
              <box height={1} width="100%">
                <text foregroundColor={statusColor.dim}>
                  {`Operations: ${selectedMount.operations.join(", ")}`}
                </text>
              </box>
            )}
          </box>
        </box>
      )}

      {/* Help bar */}
      <box height={1} width="100%">
        {loading ? (
          <text foregroundColor={statusColor.warning}>⠋ Refreshing...</text>
        ) : syncingMounts.size > 0 ? (
          <text foregroundColor={statusColor.warning}>{`⠋ Syncing ${syncingMounts.size} mount(s)...`}</text>
        ) : (
          <text foregroundColor={statusColor.dim}>
            j/k:navigate  s:sync  u:unmount  r:refresh
          </text>
        )}
      </box>
    </box>
  );
}
