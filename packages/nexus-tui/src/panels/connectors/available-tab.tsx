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
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useCopy } from "../../shared/hooks/use-copy.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { ConnectorRow } from "./connector-row.js";
import { statusColor } from "../../shared/theme.js";

/**
 * Copy text to system clipboard using platform-native tools (pbcopy/xclip)
 * and also write to a temp file so it survives TUI exit.
 */
function copyCommand(text: string): void {
  const { execSync, exec } = require("child_process");
  const fs = require("fs");

  // Write to temp file so user can retrieve after TUI exit
  try {
    fs.writeFileSync("/tmp/nexus-mount-cmd.txt", text + "\n");
  } catch {}

  // Copy to system clipboard via platform tool
  try {
    if (process.platform === "darwin") {
      execSync("pbcopy", { input: text, timeout: 2000 });
    } else {
      // Try xclip, then xsel
      try {
        execSync("xclip -selection clipboard", { input: text, timeout: 2000 });
      } catch {
        execSync("xsel --clipboard --input", { input: text, timeout: 2000 });
      }
    }
  } catch {
    // Fall back to OSC 52
    const encoded = Buffer.from(text).toString("base64");
    process.stdout.write(`\x1b]52;c;${encoded}\x07`);
  }
}

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

  const config = useGlobalStore((s) => s.config);
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

  /** Build mount command for storage connectors that need config. */
  const getMountCommand = useCallback((): string => {
    const selected = connectors[selectedIndex];
    if (!selected) return "";
    const baseName = selected.name.replace(/_connector$/, "");
    const mountPath = `/mnt/${baseName}`;
    const url = (config as Record<string, unknown>).baseUrl as string | undefined;
    const apiKey = (config as Record<string, unknown>).apiKey as string | undefined;

    // Build config template based on connector type
    let configJson = "'{}'";
    if (selected.name.includes("s3")) {
      configJson = '\'{"bucket_name": "<BUCKET>", "access_key_id": "<KEY>", "secret_access_key": "<SECRET>"}\'';
    } else if (selected.name.includes("gcs")) {
      configJson = '\'{"bucket_name": "<BUCKET>", "credentials_path": "<PATH>"}\'';
    } else if (selected.name.includes("local")) {
      configJson = '\'{"local_path": "<PATH>"}\'';
    }

    // Use eval $(nexus env) prefix so NEXUS_URL, NEXUS_API_KEY, and
    // NEXUS_GRPC_PORT are all set correctly for the CLI
    const nexusDir = process.env.NEXUS_DATA_DIR
      ? `cd ${process.env.NEXUS_DATA_DIR.replace(/\/nexus-data$/, "")} && `
      : "";
    return `${nexusDir}eval $(nexus env) && nexus mounts add ${mountPath} ${selected.name} ${configJson}`;
  }, [connectors, selectedIndex, config]);

  /** Check if connector can be mounted directly (no config needed). */
  const canDirectMount = useCallback((): boolean => {
    const selected = connectors[selectedIndex];
    if (!selected) return false;
    return selected.category === "cli" || selected.category === "oauth" || selected.category === "api";
  }, [connectors, selectedIndex]);

  // Mount success flash
  const [mountFlash, setMountFlash] = useState<string | null>(null);

  /** Mount directly via API for connectors that need no config. */
  const handleDirectMount = useCallback(async () => {
    const selected = connectors[selectedIndex];
    if (!selected) return;
    if (selected.mount_path) {
      // Already mounted — show flash
      setMountFlash(`Already mounted at ${selected.mount_path}`);
      setTimeout(() => setMountFlash(null), 2000);
      return;
    }
    const baseName = selected.name.replace(/_connector$/, "");
    setMountFlash(`Mounting ${baseName}...`);
    await mountConnector(selected.name, `/mnt/${baseName}`, client);
    setMountFlash(`✓ Mounted at /mnt/${baseName}`);
    setTimeout(() => setMountFlash(null), 2000);
  }, [connectors, selectedIndex, mountConnector, client]);

  /** Enter/m: direct mount if no config needed, otherwise show CLI guide. */
  const handleMountAction = useCallback(() => {
    if (canDirectMount()) {
      handleDirectMount();
    } else {
      setShowMountGuide(!showMountGuide);
    }
  }, [canDirectMount, handleDirectMount, showMountGuide]);

  const listNav = listNavigationBindings({
    getIndex: () => selectedIndex,
    setIndex: (i) => { setSelectedIndex(i); setShowMountGuide(false); },
    getLength: () => connectors.length,
    onSelect: handleMountAction,
  });

  useKeyboard(
    overlayActive
      ? {}
      : {
          ...listNav,
          a: handleAuth,
          m: handleMountAction,
          r: () => fetchAvailable(client),
          y: () => {
            if (showMountGuide) {
              const cmd = getMountCommand();
              copyCommand(cmd);
              copy(cmd);
            } else if (authFlow.auth_url) {
              copyCommand(authFlow.auth_url);
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
      {/* Mount flash */}
      {mountFlash && (
        <box height={1} width="100%" marginBottom={1}>
          <text foregroundColor={mountFlash.startsWith("✓") ? statusColor.healthy : statusColor.info}>
            {mountFlash}
          </text>
        </box>
      )}

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
              {"  y:copy to clipboard  Esc:close  (also saved to /tmp/nexus-mount-cmd.txt)"}
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
            <>
              <box height={1} width="100%">
                <text foregroundColor={statusColor.error}>
                  {`✕ Auth failed: ${authFlow.error_message ?? "Unknown error"}`}
                </text>
              </box>
              <box height={1} width="100%">
                <text foregroundColor={statusColor.dim}>
                  {"  To set up OAuth: configure GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET env vars,"}
                </text>
              </box>
              <box height={1} width="100%">
                <text foregroundColor={statusColor.dim}>
                  {"  then restart the server. Or mount directly: connectors like gws_gmail work without OAuth."}
                </text>
              </box>
              <box height={1} width="100%">
                <text foregroundColor={statusColor.dim}>
                  {"  a:retry  Esc:dismiss"}
                </text>
              </box>
            </>
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
