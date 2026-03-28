/**
 * Root application component.
 *
 * Lazy-loads panels on first navigation for fast startup.
 * Shows PreConnectionScreen when the server is unavailable (Decision 3A).
 */

import React, { lazy, Suspense, useState, useCallback, useEffect } from "react";
import { useGlobalStore, type PanelId } from "./stores/global-store.js";
import { useUiStore } from "./stores/ui-store.js";
import { TabBar, type Tab } from "./shared/components/tab-bar.js";
import { StatusBar } from "./shared/components/status-bar.js";
import { ErrorBar } from "./shared/components/error-bar.js";
import { ErrorBoundary } from "./shared/components/error-boundary.js";
import { Spinner } from "./shared/components/spinner.js";
import { useKeyboard } from "./shared/hooks/use-keyboard.js";
import { IdentitySwitcher } from "./shared/components/identity-switcher.js";
import { AppConfirmDialog } from "./shared/components/app-confirm-dialog.js";
import { HelpOverlay } from "./shared/components/help-overlay.js";
import { WelcomeScreen } from "./shared/components/welcome-screen.js";
import { PreConnectionScreen } from "./shared/components/pre-connection-screen.js";
import { useFreshServer } from "./shared/hooks/use-fresh-server.js";
import { detectConnectionState } from "./shared/hooks/use-connection-state.js";
import { killAllProcesses } from "./services/command-runner.js";

// Lazy-loaded panels
const FileExplorerPanel = lazy(() => import("./panels/files/file-explorer-panel.js"));
const VersionsPanel = lazy(() => import("./panels/versions/versions-panel.js"));
const AgentsPanel = lazy(() => import("./panels/agents/agents-panel.js"));
const ZonesPanel = lazy(() => import("./panels/zones/zones-panel.js"));
const AccessPanel = lazy(() => import("./panels/access/access-panel.js"));
const PaymentsPanel = lazy(() => import("./panels/payments/payments-panel.js"));
const SearchPanel = lazy(() => import("./panels/search/search-panel.js"));
const WorkflowsPanel = lazy(() => import("./panels/workflows/workflows-panel.js"));
const EventsPanel = lazy(() => import("./panels/events/events-panel.js"));
const ApiConsolePanel = lazy(() => import("./panels/api-console/api-console-panel.js"));
const ConnectorsPanel = lazy(() => import("./panels/connectors/connectors-panel.js"));

const TABS: readonly Tab[] = [
  { id: "files", label: "Files", shortcut: "1" },
  { id: "versions", label: "Ver", shortcut: "2" },
  { id: "agents", label: "Agent", shortcut: "3" },
  { id: "zones", label: "Zone", shortcut: "4" },
  { id: "access", label: "ACL", shortcut: "5" },
  { id: "payments", label: "Pay", shortcut: "6" },
  { id: "search", label: "Find", shortcut: "7" },
  { id: "workflows", label: "Flow", shortcut: "8" },
  { id: "infrastructure", label: "Event", shortcut: "9" },
  { id: "console", label: "CLI", shortcut: "0" },
  { id: "connectors", label: "Conn", shortcut: "C" },
];

function PanelRouter(): React.ReactNode {
  const activePanel = useGlobalStore((s) => s.activePanel);

  switch (activePanel) {
    case "files":
      return <FileExplorerPanel />;
    case "versions":
      return <VersionsPanel />;
    case "agents":
      return <AgentsPanel />;
    case "zones":
      return <ZonesPanel />;
    case "access":
      return <AccessPanel />;
    case "payments":
      return <PaymentsPanel />;
    case "search":
      return <SearchPanel />;
    case "workflows":
      return <WorkflowsPanel />;
    case "infrastructure":
      return <EventsPanel />;
    case "console":
      return <ApiConsolePanel />;
    case "connectors":
      return <ConnectorsPanel />;
    default:
      return (
        <box height="100%" width="100%" justifyContent="center" alignItems="center">
          <text>{`Unknown panel: "${activePanel}"`}</text>
        </box>
      );
  }
}

/**
 * Graceful shutdown: kill child processes, restore terminal, then exit (Decision 6A).
 *
 * We must manually reset the terminal before process.exit() because exit()
 * bypasses OpenTUI's renderer.destroy() cleanup, leaving mouse tracking
 * and alternate screen enabled — which causes raw escape sequences to leak
 * into the shell after exit.
 */
function shutdown(): void {
  killAllProcesses();

  // Restore stdin from raw mode first (stops reading mouse input)
  if (process.stdin.setRawMode) {
    process.stdin.setRawMode(false);
  }
  process.stdin.pause();

  // Restore terminal: disable mouse tracking, leave alternate screen, show cursor.
  // Use writeSync via fd to guarantee the sequences are flushed before exit.
  const fs = require("fs");
  const reset = [
    "\x1b[?1003l", // disable all-motion mouse tracking
    "\x1b[?1006l", // disable SGR mouse mode
    "\x1b[?1000l", // disable normal mouse tracking
    "\x1b[?1049l", // switch back to main screen
    "\x1b[?25h",   // show cursor
  ].join("");
  fs.writeSync(1, reset);

  process.exit(0);
}

export function App(): React.ReactNode {
  const activePanel = useGlobalStore((s) => s.activePanel);
  const setActivePanel = useGlobalStore((s) => s.setActivePanel);
  const connectionStatus = useGlobalStore((s) => s.connectionStatus);
  const connectionError = useGlobalStore((s) => s.connectionError);
  const config = useGlobalStore((s) => s.config);
  const toggleZoom = useUiStore((s) => s.toggleZoom);
  const zoomedPanel = useUiStore((s) => s.zoomedPanel);
  const [identitySwitcherOpen, setIdentitySwitcherOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const { isFresh } = useFreshServer();
  const [welcomeDismissed, setWelcomeDismissed] = useState(false);
  const showWelcome = isFresh === true && !welcomeDismissed;

  // Determine if we should show the pre-connection screen (Decision 3A)
  // Only hide it when fully connected — "connecting" still shows the pre-connection
  // screen with a spinner to avoid flashing the main UI during connection attempts.
  const connState = detectConnectionState(connectionStatus, connectionError, config);
  const showPreConnection = connState !== "ready";

  const setOverlayActive = useUiStore((s) => s.setOverlayActive);
  useEffect(() => {
    setOverlayActive(identitySwitcherOpen || helpOpen || showWelcome);
  }, [identitySwitcherOpen, helpOpen, showWelcome, setOverlayActive]);

  const toggleIdentitySwitcher = useCallback(() => {
    setIdentitySwitcherOpen((prev) => !prev);
  }, []);

  const closeIdentitySwitcher = useCallback(() => {
    setIdentitySwitcherOpen(false);
  }, []);

  useKeyboard(
    showPreConnection
      ? {
          // Pre-connection screen handles its own keybindings
          "q": shutdown,
        }
      : identitySwitcherOpen || helpOpen || showWelcome
      ? {
          // When an overlay is open, only dismiss keys work from app level.
          "ctrl+i": toggleIdentitySwitcher,
        }
      : {
          // Check fileEditorOpen synchronously inside each handler so we don't
          // depend on React re-render timing — OpenTUI broadcasts to ALL handlers.
          "1": () => { if (!useUiStore.getState().fileEditorOpen) setActivePanel("files"); },
          "2": () => { if (!useUiStore.getState().fileEditorOpen) setActivePanel("versions"); },
          "3": () => { if (!useUiStore.getState().fileEditorOpen) setActivePanel("agents"); },
          "4": () => { if (!useUiStore.getState().fileEditorOpen) setActivePanel("zones"); },
          "5": () => { if (!useUiStore.getState().fileEditorOpen) setActivePanel("access"); },
          "6": () => { if (!useUiStore.getState().fileEditorOpen) setActivePanel("payments"); },
          "7": () => { if (!useUiStore.getState().fileEditorOpen) setActivePanel("search"); },
          "8": () => { if (!useUiStore.getState().fileEditorOpen) setActivePanel("workflows"); },
          "9": () => { if (!useUiStore.getState().fileEditorOpen) setActivePanel("infrastructure"); },
          "0": () => { if (!useUiStore.getState().fileEditorOpen) setActivePanel("console"); },
          "shift+c": () => { if (!useUiStore.getState().fileEditorOpen) setActivePanel("connectors"); },
          "ctrl+i": toggleIdentitySwitcher,
          "ctrl+d": () => {
            // Disconnect and go back to setup menu
            useGlobalStore.getState().setConnectionStatus("error", "Disconnected by user");
          },
          "z": () => { if (!useUiStore.getState().fileEditorOpen) toggleZoom(activePanel); },
          "?": () => { if (!useUiStore.getState().fileEditorOpen) setHelpOpen(true); },
          "q": () => { if (!useUiStore.getState().fileEditorOpen) shutdown(); },
        },
  );

  // Pre-connection screen (Decision 3A): shown when server is unavailable
  if (showPreConnection) {
    return (
      <box height="100%" width="100%" flexDirection="column">
        <PreConnectionScreen />
        <StatusBar />
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Tab bar (hidden when zoomed) */}
      {!zoomedPanel && <TabBar tabs={TABS} activeTab={activePanel} onSelect={(id) => setActivePanel(id as PanelId)} />}

      {/* Main content */}
      <box flexGrow={1}>
        <ErrorBoundary>
          <Suspense
            fallback={
              <box height="100%" width="100%" justifyContent="center" alignItems="center">
                <Spinner label="Loading panel..." />
              </box>
            }
          >
            <PanelRouter />
          </Suspense>
        </ErrorBoundary>
      </box>

      {/* Overlays */}
      {showWelcome && <WelcomeScreen onDismiss={() => setWelcomeDismissed(true)} />}
      <IdentitySwitcher visible={identitySwitcherOpen} onClose={closeIdentitySwitcher} />
      <AppConfirmDialog />
      <HelpOverlay visible={helpOpen} panel={activePanel} onDismiss={() => setHelpOpen(false)} />

      {/* Error bar + Status bar */}
      <ErrorBar />
      <StatusBar />
    </box>
  );
}
