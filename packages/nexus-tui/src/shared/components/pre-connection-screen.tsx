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
import { resolveConfig, FetchClient } from "@nexus/api-client";
import { useFilesStore } from "../../stores/files-store.js";

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
  const [apiKeyWarning, setApiKeyWarning] = useState<string | null>(null);

  // Track previous commandStatus to detect completion
  const prevCommandStatus = useRef(commandStatus);
  // Track API key before init commands to detect changes
  const prevApiKey = useRef<string | undefined>(undefined);

  // When a local command finishes (success or error), re-read config from disk.
  // Behavior depends on command type:
  //   - "nexus up" success → auto-reconnect (server just started)
  //   - "nexus init" success + API key changed → warn user to restart server
  //   - "nexus demo" / "nexus up" success → clear file cache (data may have changed)
  //   - All others → stay disconnected, user presses R when ready
  useEffect(() => {
    const prev = prevCommandStatus.current;
    prevCommandStatus.current = commandStatus;

    if (
      (prev === "running") &&
      (commandStatus === "success" || commandStatus === "error")
    ) {
      const label = useCommandRunnerStore.getState().commandLabel;
      const isUpCommand = label.startsWith("nexus up");
      const isDataCommand = label.startsWith("nexus demo") || isUpCommand;
      const isInitCommand = label.startsWith("nexus init");

      // Re-read config from disk without triggering connection test.
      // resolveConfig() picks up new api_key/ports from nexus.yaml.
      const newConfig = resolveConfig({ transformKeys: false });
      const client = newConfig.apiKey ? new FetchClient(newConfig) : null;

      // #3: Detect API key change after init commands
      if (commandStatus === "success" && isInitCommand && prevApiKey.current !== undefined) {
        if (newConfig.apiKey && newConfig.apiKey !== prevApiKey.current) {
          setApiKeyWarning("API key changed. Restart server (Shift+U) to apply.");
        }
      }
      prevApiKey.current = undefined;

      // #6: Clear file cache after data-mutating commands
      if (commandStatus === "success" && isDataCommand) {
        useFilesStore.getState().clearCache();
      }

      // #1: Auto-reconnect after "nexus up" succeeds
      if (commandStatus === "success" && isUpCommand && client) {
        useGlobalStore.setState({ config: newConfig, client });
        initConfig();
      } else {
        useGlobalStore.setState({
          config: newConfig,
          client,
          // Stay disconnected — user presses R when ready
          connectionStatus: client ? "error" : "disconnected",
          connectionError: client ? "Press R to connect after setup" : null,
        });
      }
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
  const hasCommandOutput = commandStatus === "success" || commandStatus === "error";

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

  // Dismiss command output and return to menu
  const dismissOutput = useCallback(() => {
    useCommandRunnerStore.getState().reset();
  }, []);

  useKeyboard(
    isCommandRunning
      ? {}
      : hasCommandOutput
      ? {
          // After a command finishes, only allow Esc to dismiss or re-run shortcuts
          escape: dismissOutput,
          backspace: dismissOutput,
          r: handleRetry,
        }
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
            prevApiKey.current = config.apiKey;
            setApiKeyWarning(null);
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("init", []);
          },
          s: () => {
            prevApiKey.current = config.apiKey;
            setApiKeyWarning(null);
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("init", ["--preset", "shared"]);
          },
          d: () => {
            prevApiKey.current = config.apiKey;
            setApiKeyWarning(null);
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("init", ["--preset", "demo", "--force"]);
          },
          u: () => {
            // Start server (nexus up)
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("up", []);
          },
          "shift+u": () => {
            // Start server with local build (nexus up --build)
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("up", ["--build"]);
          },
          p: () => {
            // Seed demo data (nexus demo init)
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("demo", ["init"]);
          },
          c: () => {
            // Connect to a different URL
            setEditingUrl(true);
            setUrlInput(config.baseUrl ?? "http://localhost:2026");
          },
        },
    isCommandRunning ? undefined : editingUrl ? handleUnhandledKey : undefined,
  );

  // Full-screen command output view when a command is running or has output
  if (commandStatus !== "idle") {
    return (
      <box height="100%" width="100%" flexDirection="column">
        <scrollbox flexGrow={1}>
          <box flexDirection="column" width="100%" padding={1}>
            <CommandOutput />
          </box>
        </scrollbox>
        <box height={1} width="100%">
          {commandStatus === "success" ? (
            <text>
              <span foregroundColor="#4dff88" bold>{"  ✓ Done"}</span>
              <span foregroundColor="#666666">{"  │  "}</span>
              <span foregroundColor="#00d4ff">{"Esc"}</span>
              <span foregroundColor="#888888">{":back  "}</span>
              <span foregroundColor="#00d4ff">{"R"}</span>
              <span foregroundColor="#888888">{":retry"}</span>
            </text>
          ) : commandStatus === "error" ? (
            <text>
              <span foregroundColor="#ff4444" bold>{"  ✗ Failed"}</span>
              <span foregroundColor="#666666">{"  │  "}</span>
              <span foregroundColor="#00d4ff">{"Esc"}</span>
              <span foregroundColor="#888888">{":back  "}</span>
              <span foregroundColor="#00d4ff">{"R"}</span>
              <span foregroundColor="#888888">{":retry"}</span>
            </text>
          ) : (
            <text>
              <span foregroundColor="#ffaa00">{"  ◐ Running..."}</span>
            </text>
          )}
        </box>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" justifyContent="center" alignItems="center">
      <box
        flexDirection="column"
        borderStyle="double"
        width={64}
        padding={1}
      >
        {/* Logo with gradient: cyan → blue → magenta */}
        <text bold foregroundColor="#00d4ff">
          {"    _   _ _____ __  __ _   _ ____"}
        </text>
        <text bold foregroundColor="#00b8ff">
          {"   | \\ | | ____|  \\/  | | | / ___|"}
        </text>
        <text bold foregroundColor="#4d8eff">
          {"   |  \\| |  _|  >\\/< | | | \\___ \\"}
        </text>
        <text bold foregroundColor="#8066ff">
          {"   | |\\  | |___/ /\\ \\| |_| |___) |"}
        </text>
        <text bold foregroundColor="#b44dff">
          {"   |_| \\_|_____/_/  \\_\\\\___/|____/"}
        </text>
        <text>{""}</text>

        {/* Status-specific message */}
        {connState === "no-config" && (
          <>
            <text>
              <span foregroundColor="#ffaa00" bold>{"  ⚠ "}</span>
              <span foregroundColor="#ffaa00" bold>{"No API key configured"}</span>
            </text>
            <text>{""}</text>
            <text foregroundColor="#888888">{"  Set NEXUS_API_KEY or add api_key to ~/.nexus/config.yaml"}</text>
            <text foregroundColor="#888888">{"  Or press [I] to initialize a new project."}</text>
          </>
        )}

        {connState === "no-server" && (
          <>
            <text>
              <span foregroundColor="#ff4444" bold>{"  ✗ "}</span>
              <span foregroundColor="#ff4444" bold>{"Cannot connect to server"}</span>
            </text>
            <text>{""}</text>
            <text foregroundColor="#888888">{`  URL: ${config.baseUrl ?? "http://localhost:2026"}`}</text>
            {connectionError && (
              <text foregroundColor="#ff6666">{`  Error: ${connectionError}`}</text>
            )}
          </>
        )}

        {connState === "auth-failed" && (
          <>
            <text>
              <span foregroundColor="#ff4444" bold>{"  ✗ "}</span>
              <span foregroundColor="#ff4444" bold>{"Authentication failed"}</span>
            </text>
            <text>{""}</text>
            <text foregroundColor="#888888">{`  URL: ${config.baseUrl ?? "http://localhost:2026"}`}</text>
            <text foregroundColor="#ff6666">{"  Check your API key or credentials."}</text>
          </>
        )}

        {connState === "connecting" && (
          <Spinner label="  Connecting..." />
        )}

        {apiKeyWarning && (
          <>
            <text>{""}</text>
            <text foregroundColor="#ffaa00">{`  ⚠ ${apiKeyWarning}`}</text>
          </>
        )}

        <text>{""}</text>

        {/* URL editor */}
        {editingUrl && (
          <>
            <text foregroundColor="#00d4ff">{"  Enter server URL:"}</text>
            <box height={1} width="100%">
              <text foregroundColor="#ffffff">{`  > ${urlInput}\u2588`}</text>
            </box>
            <text foregroundColor="#666666">{"  Enter to connect, Esc to cancel"}</text>
            <text>{""}</text>
          </>
        )}

        {/* Actions */}
        {connState !== "connecting" && !editingUrl && (
          <>
            <text foregroundColor="#888888" bold>{"  Setup"}</text>
            <text>
              <span foregroundColor="#00d4ff" bold>{"  [I] "}</span>
              <span foregroundColor="#cccccc">{"Init local"}</span>
              <span foregroundColor="#666666">{" (nexus init)"}</span>
            </text>
            <text>
              <span foregroundColor="#00d4ff" bold>{"  [S] "}</span>
              <span foregroundColor="#cccccc">{"Init shared Docker"}</span>
              <span foregroundColor="#666666">{" (--preset shared)"}</span>
            </text>
            <text>
              <span foregroundColor="#00d4ff" bold>{"  [D] "}</span>
              <span foregroundColor="#cccccc">{"Init demo Docker"}</span>
              <span foregroundColor="#666666">{" (--preset demo)"}</span>
            </text>
            <text>
              <span foregroundColor="#4dff88" bold>{"  [U] "}</span>
              <span foregroundColor="#cccccc">{"Start server"}</span>
              <span foregroundColor="#666666">{" (nexus up)"}</span>
            </text>
            <text>
              <span foregroundColor="#4dff88" bold>{"  [⇧U] "}</span>
              <span foregroundColor="#cccccc">{"Build from source"}</span>
              <span foregroundColor="#666666">{" (nexus up --build)"}</span>
            </text>
            <text>
              <span foregroundColor="#ffaa00" bold>{"  [P] "}</span>
              <span foregroundColor="#cccccc">{"Seed demo data"}</span>
              <span foregroundColor="#666666">{" (nexus demo init)"}</span>
            </text>
            <text>{""}</text>
            <text foregroundColor="#888888" bold>{"  Connection"}</text>
            <text>
              <span foregroundColor="#b44dff" bold>{"  [C] "}</span>
              <span foregroundColor="#cccccc">{"Connect to a different URL"}</span>
            </text>
            <text>
              <span foregroundColor="#b44dff" bold>{"  [R] "}</span>
              <span foregroundColor="#cccccc">{`Retry connection${retryCount > 0 ? ` (${retryCount})` : ""}`}</span>
            </text>
            <text>
              <span foregroundColor={autoPoll ? "#4dff88" : "#888888"} bold>{"  [A] "}</span>
              <span foregroundColor={autoPoll ? "#4dff88" : "#cccccc"}>{autoPoll ? "Auto-check: ON (every 5s)" : "Enable auto-check (every 5s)"}</span>
            </text>
          </>
        )}
      </box>
    </box>
  );
}
