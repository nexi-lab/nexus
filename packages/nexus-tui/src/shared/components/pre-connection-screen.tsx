/**
 * PreConnectionScreen — shown when the server is not available (Decision 3A).
 *
 * Guides users through setup: init, start server, configure URL.
 * Supports manual retry + opt-in auto-poll (Decision 14A).
 */

import React, { useState, useEffect, useCallback } from "react";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { detectConnectionState, type ConnectionState } from "../hooks/use-connection-state.js";
import { executeLocalCommand, useCommandRunnerStore } from "../../services/command-runner.js";
import { CommandOutput } from "./command-output.js";
import { Spinner } from "./spinner.js";
import { statusColor } from "../theme.js";

const AUTO_POLL_INTERVAL = 5_000; // 5 seconds (Decision 14A)

export function PreConnectionScreen(): React.ReactNode {
  const connectionStatus = useGlobalStore((s) => s.connectionStatus);
  const connectionError = useGlobalStore((s) => s.connectionError);
  const config = useGlobalStore((s) => s.config);
  const testConnection = useGlobalStore((s) => s.testConnection);

  const commandStatus = useCommandRunnerStore((s) => s.status);

  const connState = detectConnectionState(connectionStatus, connectionError, config);

  const [autoPoll, setAutoPoll] = useState(false);
  const [retryCount, setRetryCount] = useState(0);

  // Manual retry
  const handleRetry = useCallback(() => {
    setRetryCount((c) => c + 1);
    testConnection();
  }, [testConnection]);

  // Auto-poll (Decision 14A: opt-in after first manual retry)
  useEffect(() => {
    if (!autoPoll || connState === "ready") return;

    const timer = setInterval(() => {
      testConnection();
    }, AUTO_POLL_INTERVAL);

    return () => clearInterval(timer);
  }, [autoPoll, connState, testConnection]);

  // Stop auto-poll when connected
  useEffect(() => {
    if (connState === "ready") {
      setAutoPoll(false);
    }
  }, [connState]);

  const isCommandRunning = commandStatus === "running";

  useKeyboard(
    isCommandRunning
      ? {}
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
        },
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
            <text dimColor>{"  Or run nexus init to create a new project."}</text>
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

        {/* Actions */}
        {connState !== "connecting" && !isCommandRunning && (
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
