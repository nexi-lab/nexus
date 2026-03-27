/**
 * Available tab: lists all registered connectors with auth and mount status.
 *
 * Supports: connector list navigation, auth initiation (opens browser),
 * auth status polling, CLI mount guidance.
 *
 * Mounting connectors requires configuration (credentials, bucket names, etc.)
 * that varies per connector. Instead of trying to collect all config in the TUI,
 * we show the CLI command the user should run, with the required arguments
 * pre-filled from the connector's connection_args.
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

  const { copy, copied } = useCopy();
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Show CLI mount guide for selected connector
  const [showMountGuide, setShowMountGuide] = useState(false);

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

    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, [authFlow.status, client, pollAuthStatus]);

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

  /** Build CLI mount command for the selected connector. */
  const getMountCommand = useCallback((): string => {
    const selected = connectors[selectedIndex];
    if (!selected) return "";
    const baseName = selected.name.replace(/_connector$/, "");
    const mountPath = `/mnt/${baseName}`;

    // Build a config hint based on connector category
    let configHint = "{}";
    if (selected.category === "storage") {
      if (selected.name.includes("s3")) {
        configHint = '\'{"bucket_name": "<BUCKET>", "access_key_id": "<KEY>", "secret_access_key": "<SECRET>"}\'';
      } else if (selected.name.includes("gcs")) {
        configHint = '\'{"bucket_name": "<BUCKET>", "credentials_path": "<PATH>"}\'';
      } else if (selected.name.includes("local")) {
        configHint = '\'{"local_path": "<PATH>"}\'';
      }
    } else if (selected.category === "cli" || selected.category === "oauth") {
      configHint = "'{}'";
    }

    return `nexus mounts add ${mountPath} ${selected.name} ${configHint}`;
  }, [connectors, selectedIndex]);

  const listNav = listNavigationBindings({
    getIndex: () => selectedIndex,
    setIndex: (i) => { setSelectedIndex(i); setShowMountGuide(false); },
    getLength: () => connectors.length,
    onSelect: () => setShowMountGuide(!showMountGuide),
  });

  useKeyboard(
    overlayActive
      ? {}
      : {
          ...listNav,
          a: handleAuth,
          m: () => setShowMountGuide(!showMountGuide),
          r: () => fetchAvailable(client),
          y: () => {
            if (showMountGuide) {
              copy(getMountCommand());
            } else if (authFlow.auth_url) {
              copy(authFlow.auth_url);
            }
          },
          escape: () => {
            if (showMountGuide) {
              setShowMountGuide(false);
            } else if (authFlow.status !== "idle") {
              cancelAuth();
            }
          },
        },
  );

  if (loading && connectors.length === 0) {
    return <LoadingIndicator message="Loading connectors..." />;
  }

  const selectedConnector = connectors[selectedIndex];

  return (
    <box flexDirection="column" height="100%" width="100%">
      {/* Mount CLI guide */}
      {showMountGuide && selectedConnector && (
        <box flexDirection="column" width="100%" borderStyle="single" marginBottom={1}>
          <box height={1} width="100%">
            <text bold foregroundColor={statusColor.info}>
              {`Mount ${selectedConnector.name.replace(/_connector$/, "")}:`}
            </text>
          </box>
          <box height={1} width="100%">
            <text foregroundColor={statusColor.dim}>Run this command in your terminal:</text>
          </box>
          <box height={1} width="100%">
            <text>
              <span foregroundColor={statusColor.healthy}>{"  $ "}</span>
              <span>{getMountCommand()}</span>
            </text>
          </box>
          <box height={1} width="100%">
            <text foregroundColor={statusColor.dim}>
              {"  Press y to copy command, Esc to close"}
            </text>
          </box>
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
            j/k:navigate  a:auth  m:mount guide  r:refresh  y:copy  Esc:cancel
          </text>
        )}
      </box>
    </box>
  );
}
