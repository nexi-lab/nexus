/**
 * Available tab: lists all registered connectors with auth status.
 *
 * Supports: connector list navigation, auth initiation (opens browser),
 * auth status polling, mount path configuration with custom path input.
 */

import React, { useState, useEffect, useRef, useCallback } from "react";
import type { FetchClient } from "@nexus/api-client";
import { useConnectorsStore } from "../../stores/connectors-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { ConnectorRow } from "./connector-row.js";
import { statusColor } from "../../shared/theme.js";

interface AvailableTabProps {
  readonly client: FetchClient;
  readonly overlayActive: boolean;
}

const AUTH_POLL_INTERVAL = 3000;

export function AvailableTab({ client, overlayActive }: AvailableTabProps): React.ReactNode {
  const connectors = useConnectorsStore((s) => s.availableConnectors);
  const loading = useConnectorsStore((s) => s.availableLoading);
  const selectedIndex = useConnectorsStore((s) => s.selectedAvailableIndex);
  const authFlow = useConnectorsStore((s) => s.authFlow);

  const setSelectedIndex = useConnectorsStore((s) => s.setSelectedAvailableIndex);
  const fetchAvailable = useConnectorsStore((s) => s.fetchAvailable);
  const initiateAuth = useConnectorsStore((s) => s.initiateAuth);
  const pollAuthStatus = useConnectorsStore((s) => s.pollAuthStatus);
  const cancelAuth = useConnectorsStore((s) => s.cancelAuth);
  const mountConnector = useConnectorsStore((s) => s.mountConnector);

  const { copy, copied } = useCopy();
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Mount path input mode
  const [mountInputMode, setMountInputMode] = useState(false);
  const [mountPathBuffer, setMountPathBuffer] = useState("");

  // Auto-fetch on mount
  useEffect(() => {
    if (connectors.length === 0) {
      fetchAvailable(client);
    }
  }, [client, connectors.length, fetchAvailable]);

  // Auth polling lifecycle
  useEffect(() => {
    if (authFlow.status === "polling" || authFlow.status === "waiting") {
      pollTimerRef.current = setInterval(() => {
        pollAuthStatus(client);
      }, AUTH_POLL_INTERVAL);

      return () => {
        if (pollTimerRef.current) clearInterval(pollTimerRef.current);
      };
    }

    // Stop polling when flow completes/errors/cancels
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, [authFlow.status, client, pollAuthStatus]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    };
  }, []);

  const handleAuth = useCallback(() => {
    const selected = connectors[selectedIndex];
    if (selected) {
      initiateAuth(selected.name, client);
    }
  }, [connectors, selectedIndex, initiateAuth, client]);

  /** Start mount flow — pre-fill with default path, allow user to edit. */
  const startMountInput = useCallback(() => {
    const selected = connectors[selectedIndex];
    if (!selected) return;
    const baseName = selected.name.replace(/_connector$/, "");
    setMountPathBuffer(`/mnt/${baseName}`);
    setMountInputMode(true);
  }, [connectors, selectedIndex]);

  /** Submit the mount with the user-specified path. */
  const submitMount = useCallback(() => {
    const selected = connectors[selectedIndex];
    if (!selected) return;
    const path = mountPathBuffer.trim();
    if (!path) return;
    mountConnector(selected.name, path, client);
    setMountInputMode(false);
    setMountPathBuffer("");
  }, [connectors, selectedIndex, mountPathBuffer, mountConnector, client]);

  const listNav = listNavigationBindings({
    getIndex: () => selectedIndex,
    setIndex: setSelectedIndex,
    getLength: () => connectors.length,
  });

  useKeyboard(
    overlayActive
      ? {}
      : mountInputMode
        ? {
            return: submitMount,
            escape: () => { setMountInputMode(false); setMountPathBuffer(""); },
            backspace: () => { setMountPathBuffer((b) => b.slice(0, -1)); },
          }
        : {
            ...listNav,
            a: handleAuth,
            m: startMountInput,
            r: () => fetchAvailable(client),
            y: () => {
              if (authFlow.auth_url) {
                copy(authFlow.auth_url);
              }
            },
            escape: () => {
              if (authFlow.status !== "idle") {
                cancelAuth();
              }
            },
          },
    // Capture typed characters in mount input mode
    (!overlayActive && mountInputMode)
      ? (keyName: string) => {
          if (keyName === "space") {
            setMountPathBuffer((b) => b + " ");
          } else if (keyName.length === 1) {
            setMountPathBuffer((b) => b + keyName);
          }
        }
      : undefined,
  );

  if (loading && connectors.length === 0) {
    return <LoadingIndicator message="Loading connectors..." />;
  }

  return (
    <box flexDirection="column" height="100%" width="100%">
      {/* Mount path input */}
      {mountInputMode && (
        <box height={1} width="100%" marginBottom={1}>
          <text>
            <span foregroundColor={statusColor.info}>{"Mount path: "}</span>
            <span bold>{mountPathBuffer}</span>
            <span foregroundColor={statusColor.info}>{"\u2588"}</span>
            <span foregroundColor={statusColor.dim}>{"  (Enter:mount  Escape:cancel)"}</span>
          </text>
        </box>
      )}

      {/* Auth flow banner */}
      {authFlow.status !== "idle" && (
        <box flexDirection="column" width="100%" borderStyle="single" marginBottom={1}>
          {authFlow.status === "waiting" && authFlow.auth_url && (
            <>
              <box height={1} width="100%">
                <text foregroundColor={statusColor.warning}>
                  {`Auth URL (press y to copy): ${authFlow.auth_url.substring(0, 60)}...`}
                </text>
              </box>
              <box height={1} width="100%">
                <text foregroundColor={statusColor.dim}>
                  {authFlow.error_message || "Open this URL in your browser to authenticate."}
                </text>
              </box>
            </>
          )}
          {authFlow.status === "polling" && (
            <box height={1} width="100%">
              <text foregroundColor={statusColor.info}>
                {`⠋ Waiting for ${authFlow.connector_name} authentication... (Escape to cancel)`}
              </text>
            </box>
          )}
          {authFlow.status === "completed" && (
            <box height={1} width="100%">
              <text foregroundColor={statusColor.healthy}>
                {`✓ ${authFlow.connector_name} authenticated successfully!`}
              </text>
            </box>
          )}
          {authFlow.status === "error" && (
            <box height={1} width="100%">
              <text foregroundColor={statusColor.error}>
                {`✕ Auth failed: ${authFlow.error_message ?? "Unknown error"} (press a to retry)`}
              </text>
            </box>
          )}
        </box>
      )}

      {/* Connector list */}
      <box flexGrow={1} flexDirection="column">
        {connectors.length === 0 ? (
          <box height={1} width="100%">
            <text foregroundColor={statusColor.dim}>No connectors registered.</text>
          </box>
        ) : (
          connectors.map((c, i) => (
            <ConnectorRow
              key={c.name}
              name={c.name}
              category={c.category}
              authStatus={c.auth_status}
              mountPath={c.mount_path}
              syncStatus={c.sync_status}
              selected={i === selectedIndex}
              showAuth={true}
              showSync={true}
            />
          ))
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        {copied ? (
          <text foregroundColor={statusColor.healthy}>Copied!</text>
        ) : (
          <text foregroundColor={statusColor.dim}>
            j/k:navigate  a:auth  m:mount  r:refresh  y:copy auth URL  Esc:cancel auth
          </text>
        )}
      </box>
    </box>
  );
}
