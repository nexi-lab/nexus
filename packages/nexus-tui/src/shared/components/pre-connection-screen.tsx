/**
 * PreConnectionScreen — shown when the server is not available (Decision 3A).
 *
 * Guides users through setup: init, start server, configure URL.
 * Supports manual retry + opt-in auto-poll (Decision 14A).
 *
 * Fix (Codex review finding 1): Retry now calls initConfig() instead of
 * testConnection() so config is re-read from disk after nexus init creates
 * nexus.yaml.  Also auto-reloads config when a local command completes.
 */

import React, { useState, useEffect, useCallback, useRef } from "react";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { detectConnectionState } from "../hooks/use-connection-state.js";
import { executeLocalCommand, useCommandRunnerStore } from "../../services/command-runner.js";
import { CommandOutput } from "./command-output.js";
import { Spinner } from "./spinner.js";
import { statusColor } from "../theme.js";

const AUTO_POLL_INTERVAL = 5_000; // 5 seconds (Decision 14A)

export function PreConnectionScreen(): React.ReactNode {
  const connectionStatus = useGlobalStore((s) => s.connectionStatus);
  const connectionError = useGlobalStore((s) => s.connectionError);
  const config = useGlobalStore((s) => s.config);
  const initConfig = useGlobalStore((s) => s.initConfig);

  const commandStatus = useCommandRunnerStore((s) => s.status);

  const connState = detectConnectionState(connectionStatus, connectionError, config);

  const [autoPoll, setAutoPoll] = useState(false);
  const [retryCount, setRetryCount] = useState(0);
  const [urlInput, setUrlInput] = useState("");
  const [editingUrl, setEditingUrl] = useState(false);

  // Track previous commandStatus to detect completion
  const prevCommandStatus = useRef(commandStatus);

  // When a local command finishes (success or error), re-read config from disk
  // so that `nexus init` creating nexus.yaml is picked up automatically.
  useEffect(() => {
    const prev = prevCommandStatus.current;
    prevCommandStatus.current = commandStatus;

    if (
      (prev === "running") &&
      (commandStatus === "success" || commandStatus === "error")
    ) {
      // Re-read config from disk — initConfig() calls resolveConfig() which
      // searches ./nexus.yaml → ~/.nexus/config.yaml, then creates a new
      // FetchClient if an API key is now present.
      initConfig();
    }
  }, [commandStatus, initConfig]);

  // Manual retry: re-read config from disk + test connection.
  // This is critical for the no-config → init → retry flow: after nexus init
  // writes nexus.yaml, we must call initConfig() (not just testConnection())
  // because testConnection() returns immediately when client=null.
  const handleRetry = useCallback(() => {
    setRetryCount((c) => c + 1);
    initConfig();
  }, [initConfig]);

  // Auto-poll: also uses initConfig() so it picks up new config from disk
  useEffect(() => {
    if (!autoPoll || connState === "ready") return;

    const timer = setInterval(() => {
      initConfig();
    }, AUTO_POLL_INTERVAL);

    return () => clearInterval(timer);
  }, [autoPoll, connState, initConfig]);

  // Stop auto-poll when connected
  useEffect(() => {
    if (connState === "ready") {
      setAutoPoll(false);
    }
  }, [connState]);

  // Connect to a different URL
  const handleConnectUrl = useCallback(() => {
    const url = urlInput.trim();
    if (!url) return;
    setEditingUrl(false);
    initConfig({ baseUrl: url });
  }, [urlInput, initConfig]);

  const isCommandRunning = commandStatus === "running";

  // Handle printable chars when editing URL
  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      if (!editingUrl) return;
      if (keyName.length === 1) {
        setUrlInput((u) => u + keyName);
      } else if (keyName === "space") {
        setUrlInput((u) => u + " ");
      }
    },
    [editingUrl],
  );

  useKeyboard(
    isCommandRunning
      ? {}
      : editingUrl
      ? {
          return: handleConnectUrl,
          escape: () => { setEditingUrl(false); setUrlInput(""); },
          backspace: () => setUrlInput((u) => u.slice(0, -1)),
        }
      : {
          r: handleRetry,
          a: () => setAutoPoll((prev) => !prev),
          i: () => {
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("init", []);
          },
          s: () => {
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("init", ["--preset", "shared"]);
          },
          u: () => {
            // Start server (nexus up)
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("init", ["--preset", "shared"]);
          },
          c: () => {
            // Connect to a different URL
            setEditingUrl(true);
            setUrlInput(config.baseUrl ?? "http://localhost:2026");
          },
        },
    isCommandRunning ? undefined : editingUrl ? handleUnhandledKey : undefined,
  );

  return (
    <box height="100%" width="100%" justifyContent="center" alignItems="center">
      <box
        flexDirection="column"
        borderStyle="double"
        width={64}
        padding={1}
      >
        <text bold foregroundColor={statusColor.info}>
          {"    \u2554\u2557\u2554\u250C\u2500\u2510\u2500\u2510 \u2510\u252C\u2510 \u252C\u250C\u2500\u2510"}
        </text>
        <text bold foregroundColor={statusColor.info}>
          {"    \u2551\u2551\u2551\u251C\u2524 \u250C\u2524 \u2502 \u2502\u2502\u2514\u2500\u2510"}
        </text>
        <text bold foregroundColor={statusColor.info}>
          {"    \u255D\u255A\u255D\u2514\u2500\u2518\u2518\u2514 \u2514\u2500\u2518\u2514\u2500\u2518"}
        </text>
        <text>{""}</text>

        {/* Status-specific message */}
        {connState === "no-config" && (
          <>
            <text foregroundColor={statusColor.warning}>{"  No API key configured"}</text>
            <text>{""}</text>
            <text dimColor>{"  Set NEXUS_API_KEY or add api_key to ~/.nexus/config.yaml"}</text>
            <text dimColor>{"  Or press [I] to initialize a new project."}</text>
          </>
        )}

        {connState === "no-server" && (
          <>
            <text foregroundColor={statusColor.error}>{"  Cannot connect to server"}</text>
            <text>{""}</text>
            <text dimColor>{`  URL: ${config.baseUrl ?? "http://localhost:2026"}`}</text>
            {connectionError && (
              <text dimColor>{`  Error: ${connectionError}`}</text>
            )}
          </>
        )}

        {connState === "auth-failed" && (
          <>
            <text foregroundColor={statusColor.error}>{"  Authentication failed"}</text>
            <text>{""}</text>
            <text dimColor>{`  URL: ${config.baseUrl ?? "http://localhost:2026"}`}</text>
            <text dimColor>{"  Check your API key or credentials."}</text>
          </>
        )}

        {connState === "connecting" && (
          <Spinner label="  Connecting..." />
        )}

        <text>{""}</text>

        {/* URL editor */}
        {editingUrl && (
          <>
            <text>{"  Enter server URL:"}</text>
            <box height={1} width="100%">
              <text>{`  > ${urlInput}\u2588`}</text>
            </box>
            <text dimColor>{"  Enter to connect, Esc to cancel"}</text>
            <text>{""}</text>
          </>
        )}

        {/* Actions */}
        {connState !== "connecting" && !isCommandRunning && !editingUrl && (
          <>
            <text>
              <text foregroundColor={statusColor.info}>{"  [I]"}</text>
              <text>{" Initialize local project (nexus init)"}</text>
            </text>
            <text>
              <text foregroundColor={statusColor.info}>{"  [S]"}</text>
              <text>{" Initialize shared project (nexus init --preset shared)"}</text>
            </text>
            <text>
              <text foregroundColor={statusColor.info}>{"  [C]"}</text>
              <text>{" Connect to a different URL"}</text>
            </text>
            <text>
              <text foregroundColor={statusColor.info}>{"  [R]"}</text>
              <text>{` Retry connection${retryCount > 0 ? ` (${retryCount})` : ""}`}</text>
            </text>
            <text>
              <text foregroundColor={autoPoll ? statusColor.success : statusColor.dim}>{"  [A]"}</text>
              <text>{autoPoll ? " Auto-check: ON (every 5s)" : " Enable auto-check (every 5s)"}</text>
            </text>
          </>
        )}

        {/* Command output (when running nexus init etc.) */}
        {commandStatus !== "idle" && (
          <>
            <text>{""}</text>
            <CommandOutput />
          </>
        )}
      </box>
    </box>
  );
}
