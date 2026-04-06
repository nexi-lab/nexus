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

import { createSignal, createEffect, createMemo, onCleanup, Show } from "solid-js";
import type { JSX } from "solid-js";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { useGlobalStore } from "../../stores/global-store.js";
import { detectConnectionState } from "../hooks/use-connection-state.js";
import { executeLocalCommand, useCommandRunnerStore } from "../../services/command-runner.js";
import { CommandOutput } from "./command-output.js";
import { Spinner } from "./spinner.js";
import { statusColor } from "../theme.js";
import { resolveConfig, FetchClient } from "@nexus-ai-fs/api-client";
import { useFilesStore } from "../../stores/files-store.js";
import { textStyle } from "../text-style.js";

const AUTO_POLL_INTERVAL = 5_000; // 5 seconds (Decision 14A)

export function PreConnectionScreen(): JSX.Element {
  const connectionStatus = useGlobalStore((s) => s.connectionStatus);
  const connectionError = useGlobalStore((s) => s.connectionError);
  const config = useGlobalStore((s) => s.config);
  const initConfig = useGlobalStore((s) => s.initConfig);

  // connState must be a memo so effects/JSX react when connectionStatus changes
  const connState = createMemo(() =>
    detectConnectionState(
      useGlobalStore((s) => s.connectionStatus),
      useGlobalStore((s) => s.connectionError),
      useGlobalStore((s) => s.config),
    )
  );

  const [autoPoll, setAutoPoll] = createSignal(false);
  const [retryCount, setRetryCount] = createSignal(0);
  const [urlInput, setUrlInput] = createSignal("");
  const [editingUrl, setEditingUrl] = createSignal(false);
  const [apiKeyWarning, setApiKeyWarning] = createSignal<string | null>(null);

  // Track previous commandStatus to detect completion (read reactively inside effect)
  let prevCommandStatus = useCommandRunnerStore.getState().status;
  // Track API key before init commands to detect changes
  let prevApiKey: string | undefined = undefined;

  // When a local command finishes (success or error), re-read config from disk.
  createEffect(() => {
    const commandStatus = useCommandRunnerStore((s) => s.status);
    const prev = prevCommandStatus;
    prevCommandStatus = commandStatus;

    if (
      (prev === "running") &&
      (commandStatus === "success" || commandStatus === "error")
    ) {
      const label = useCommandRunnerStore.getState().commandLabel;
      const isUpCommand = label.startsWith("nexus up");
      const isDataCommand = label.startsWith("nexus demo") || isUpCommand;
      const isInitCommand = label.startsWith("nexus init");

      const newConfig = resolveConfig({ transformKeys: false });
      const client = new FetchClient(newConfig);

      if (commandStatus === "success" && isInitCommand && prevApiKey !== undefined) {
        if (newConfig.apiKey && newConfig.apiKey !== prevApiKey) {
          setApiKeyWarning("API key changed. Restart server (Shift+U) to apply.");
        }
      }
      prevApiKey = undefined;

      if (commandStatus === "success" && isDataCommand) {
        useFilesStore.getState().clearCache();
      }

      if (commandStatus === "success" && isUpCommand && client) {
        useGlobalStore.setState({ config: newConfig, client });
        initConfig();
      } else {
        useGlobalStore.setState({
          config: newConfig,
          client,
          connectionStatus: "error",
          connectionError: "Press R to connect after setup",
        });
      }
    }
  });

  const handleRetry = () => {
    setRetryCount((c) => c + 1);
    initConfig();
  };

  // Auto-poll: opt-in (user presses A). Uses onCleanup — NOT return value —
  // because SolidJS createEffect does not treat return values as cleanup.
  createEffect(() => {
    if (!autoPoll() || connState() === "ready") return;

    const timer = setInterval(() => {
      initConfig();
    }, AUTO_POLL_INTERVAL);

    onCleanup(() => clearInterval(timer));
  });

  // Stop auto-poll when connected
  createEffect(() => {
    if (connState() === "ready") {
      setAutoPoll(false);
    }
  });

  const handleConnectUrl = () => {
    const url = urlInput().trim();
    if (!url) return;
    setEditingUrl(false);
    initConfig({ baseUrl: url });
  };

  // Reactive store accessors (direct reads via jsx:preserve)
  const commandStatus = () => useCommandRunnerStore((s) => s.status);
  const isCommandRunning = () => commandStatus() === "running";
  const hasCommandOutput = () => {
    const s = commandStatus();
    return s === "success" || s === "error";
  };

  // Handle printable chars when editing URL
  const handleUnhandledKey = (keyName: string) => {
      if (!editingUrl) return;
      if (keyName.length === 1) {
        setUrlInput((u) => u + keyName);
      } else if (keyName === "space") {
        setUrlInput((u) => u + " ");
      }
    };

  // Dismiss command output and return to menu
  const dismissOutput = () => {
    useCommandRunnerStore.getState().reset();
  };

  useKeyboard(
    isCommandRunning()
      ? {}
      : hasCommandOutput()
      ? {
          // After a command finishes, only allow Esc to dismiss or re-run shortcuts
          escape: dismissOutput,
          backspace: dismissOutput,
          r: handleRetry,
        }
      : editingUrl()
      ? {
          return: handleConnectUrl,
          escape: () => { setEditingUrl(false); setUrlInput(""); },
          backspace: () => setUrlInput((u) => u.slice(0, -1)),
        }
      : {
          r: handleRetry,
          a: () => setAutoPoll((prev) => !prev),
          i: () => {
            prevApiKey = config.apiKey;
            setApiKeyWarning(null);
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("init", []);
          },
          s: () => {
            prevApiKey = config.apiKey;
            setApiKeyWarning(null);
            useCommandRunnerStore.getState().reset();
            executeLocalCommand("init", ["--preset", "shared"]);
          },
          d: () => {
            prevApiKey = config.apiKey;
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
            setUrlInput(useGlobalStore.getState().config.baseUrl ?? "http://localhost:2026");
          },
        },
    isCommandRunning() ? undefined : editingUrl() ? handleUnhandledKey : undefined,
  );

  // Full-screen command output view when a command is running or has output.
  // Use <Show> for reactive switching (SolidJS: if/return evaluates once).
  return (
    <Show when={commandStatus() !== "idle"} fallback={
    <box height="100%" width="100%" justifyContent="center" alignItems="center">
      <box
        flexDirection="column"
        borderStyle="double"
        width={64}
        padding={1}
      >
        {/* Logo with gradient: cyan → blue → magenta */}
        <text style={textStyle({ fg: "#00d4ff", bold: true })}>
          {"    _   _ _____ __  __ _   _ ____"}
        </text>
        <text style={textStyle({ fg: "#00b8ff", bold: true })}>
          {"   | \\ | | ____|  \\/  | | | / ___|"}
        </text>
        <text style={textStyle({ fg: "#4d8eff", bold: true })}>
          {"   |  \\| |  _|  >\\/< | | | \\___ \\"}
        </text>
        <text style={textStyle({ fg: "#8066ff", bold: true })}>
          {"   | |\\  | |___/ /\\ \\| |_| |___) |"}
        </text>
        <text style={textStyle({ fg: "#b44dff", bold: true })}>
          {"   |_| \\_|_____/_/  \\_\\\\___/|____/"}
        </text>
        <text>{""}</text>

        {/* Status-specific message */}
        {connState() === "no-config" && (
          <>
            <text>
              <span style={textStyle({ fg: "#ffaa00", bold: true })}>{"  ⚠ "}</span>
              <span style={textStyle({ fg: "#ffaa00", bold: true })}>{"No API key configured"}</span>
            </text>
            <text>{""}</text>
            <text style={textStyle({ fg: "#888888" })}>{"  Set NEXUS_API_KEY or add api_key to ~/.nexus/config.yaml"}</text>
            <text style={textStyle({ fg: "#888888" })}>{"  Or press [I] to initialize a new project."}</text>
          </>
        )}

        {connState() === "no-server" && (
          <>
            <text>
              <span style={textStyle({ fg: "#ff4444", bold: true })}>{"  ✗ "}</span>
              <span style={textStyle({ fg: "#ff4444", bold: true })}>{"Cannot connect to server"}</span>
            </text>
            <text>{""}</text>
            <text style={textStyle({ fg: "#888888" })}>{`  URL: ${useGlobalStore((s) => s.config).baseUrl ?? "http://localhost:2026"}`}</text>
            {useGlobalStore((s) => s.connectionError) && (
              <text style={textStyle({ fg: "#ff6666" })}>{`  Error: ${useGlobalStore((s) => s.connectionError)}`}</text>
            )}
          </>
        )}

        {connState() === "auth-failed" && (
          <>
            <text>
              <span style={textStyle({ fg: "#ff4444", bold: true })}>{"  ✗ "}</span>
              <span style={textStyle({ fg: "#ff4444", bold: true })}>{"Authentication failed"}</span>
            </text>
            <text>{""}</text>
            <text style={textStyle({ fg: "#888888" })}>{`  URL: ${useGlobalStore((s) => s.config).baseUrl ?? "http://localhost:2026"}`}</text>
            <text style={textStyle({ fg: "#ff6666" })}>{"  Check your API key or credentials."}</text>
          </>
        )}

        {connState() === "connecting" && (
          <Spinner label="  Connecting..." />
        )}

        {apiKeyWarning() && (
          <>
            <text>{""}</text>
            <text style={textStyle({ fg: "#ffaa00" })}>{`  ⚠ ${apiKeyWarning()}`}</text>
          </>
        )}

        <text>{""}</text>

        {/* URL editor */}
        {editingUrl() && (
          <>
            <text style={textStyle({ fg: "#00d4ff" })}>{"  Enter server URL:"}</text>
            <box height={1} width="100%">
              <text style={textStyle({ fg: "#ffffff" })}>{`  > ${urlInput()}\u2588`}</text>
            </box>
            <text style={textStyle({ fg: "#666666" })}>{"  Enter to connect, Esc to cancel"}</text>
            <text>{""}</text>
          </>
        )}

        {/* Actions */}
        {connState() !== "connecting" && !editingUrl() && (
          <>
            <text style={textStyle({ fg: "#888888", bold: true })}>{"  Setup"}</text>
            <text>
              <span style={textStyle({ fg: "#00d4ff", bold: true })}>{"  [I] "}</span>
              <span style={textStyle({ fg: "#cccccc" })}>{"Init local"}</span>
              <span style={textStyle({ fg: "#666666" })}>{" (nexus init)"}</span>
            </text>
            <text>
              <span style={textStyle({ fg: "#00d4ff", bold: true })}>{"  [S] "}</span>
              <span style={textStyle({ fg: "#cccccc" })}>{"Init shared Docker"}</span>
              <span style={textStyle({ fg: "#666666" })}>{" (--preset shared)"}</span>
            </text>
            <text>
              <span style={textStyle({ fg: "#00d4ff", bold: true })}>{"  [D] "}</span>
              <span style={textStyle({ fg: "#cccccc" })}>{"Init demo Docker"}</span>
              <span style={textStyle({ fg: "#666666" })}>{" (--preset demo)"}</span>
            </text>
            <text>
              <span style={textStyle({ fg: "#4dff88", bold: true })}>{"  [U] "}</span>
              <span style={textStyle({ fg: "#cccccc" })}>{"Start server"}</span>
              <span style={textStyle({ fg: "#666666" })}>{" (nexus up)"}</span>
            </text>
            <text>
              <span style={textStyle({ fg: "#4dff88", bold: true })}>{"  [⇧U] "}</span>
              <span style={textStyle({ fg: "#cccccc" })}>{"Build from source"}</span>
              <span style={textStyle({ fg: "#666666" })}>{" (nexus up --build)"}</span>
            </text>
            <text>
              <span style={textStyle({ fg: "#ffaa00", bold: true })}>{"  [P] "}</span>
              <span style={textStyle({ fg: "#cccccc" })}>{"Seed demo data"}</span>
              <span style={textStyle({ fg: "#666666" })}>{" (nexus demo init)"}</span>
            </text>
            <text>{""}</text>
            <text style={textStyle({ fg: "#888888", bold: true })}>{"  Connection"}</text>
            <text>
              <span style={textStyle({ fg: "#b44dff", bold: true })}>{"  [C] "}</span>
              <span style={textStyle({ fg: "#cccccc" })}>{"Connect to a different URL"}</span>
            </text>
            <text>
              <span style={textStyle({ fg: "#b44dff", bold: true })}>{"  [R] "}</span>
              <span style={textStyle({ fg: "#cccccc" })}>{`Retry connection${retryCount() > 0 ? ` (${retryCount()})` : ""}`}</span>
            </text>
            <text>
              <span style={textStyle({ fg: autoPoll() ? "#4dff88" : "#888888", bold: true })}>{"  [A] "}</span>
              <span style={textStyle({ fg: autoPoll() ? "#4dff88" : "#cccccc" })}>{autoPoll() ? "Auto-check: ON (every 5s)" : "Enable auto-check (every 5s)"}</span>
            </text>
          </>
        )}
      </box>
    </box>
    }>
      <box height="100%" width="100%" flexDirection="column">
        <scrollbox flexGrow={1}>
          <box flexDirection="column" width="100%" padding={1}>
            <CommandOutput />
          </box>
        </scrollbox>
        <box height={1} width="100%">
          {commandStatus() === "success" ? (
            <text>
              <span style={textStyle({ fg: "#4dff88", bold: true })}>{"  ✓ Done"}</span>
              <span style={textStyle({ fg: "#666666" })}>{"  │  "}</span>
              <span style={textStyle({ fg: "#00d4ff" })}>{"Esc"}</span>
              <span style={textStyle({ fg: "#888888" })}>{":back  "}</span>
              <span style={textStyle({ fg: "#00d4ff" })}>{"R"}</span>
              <span style={textStyle({ fg: "#888888" })}>{":retry"}</span>
            </text>
          ) : commandStatus() === "error" ? (
            <text>
              <span style={textStyle({ fg: "#ff4444", bold: true })}>{"  ✗ Failed"}</span>
              <span style={textStyle({ fg: "#666666" })}>{"  │  "}</span>
              <span style={textStyle({ fg: "#00d4ff" })}>{"Esc"}</span>
              <span style={textStyle({ fg: "#888888" })}>{":back  "}</span>
              <span style={textStyle({ fg: "#00d4ff" })}>{"R"}</span>
              <span style={textStyle({ fg: "#888888" })}>{":retry"}</span>
            </text>
          ) : (
            <text>
              <span style={textStyle({ fg: "#ffaa00" })}>{"  ◐ Running..."}</span>
            </text>
          )}
        </box>
      </box>
    </Show>
  );
}
