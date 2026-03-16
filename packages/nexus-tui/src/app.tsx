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

const TABS: readonly Tab[] = [
  { id: "files", label: "Files", shortcut: "1" },
  { id: "versions", label: "Versions", shortcut: "2" },
  { id: "agents", label: "Agents", shortcut: "3" },
  { id: "zones", label: "Zones", shortcut: "4" },
  { id: "access", label: "Access", shortcut: "5" },
  { id: "payments", label: "Pay", shortcut: "6" },
  { id: "search", label: "Search", shortcut: "7" },
  { id: "workflows", label: "Workflows", shortcut: "8" },
  { id: "infrastructure", label: "Events", shortcut: "9" },
  { id: "console", label: "Console", shortcut: "0" },
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
    default:
      return (
        <box height="100%" width="100%" justifyContent="center" alignItems="center">
          <text>{`Unknown panel: "${activePanel}"`}</text>
        </box>
      );
  }
}

/**
 * Graceful shutdown: kill child processes then exit (Decision 6A).
 */
function shutdown(): void {
  killAllProcesses();
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
  const connState = detectConnectionState(connectionStatus, connectionError, config);
  const showPreConnection = connState !== "ready" && connState !== "connecting";

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
          // When an overlay is open, only its dismiss key works from app level.
          "ctrl+i": toggleIdentitySwitcher,
        }
      : {
          "1": () => setActivePanel("files"),
          "2": () => setActivePanel("versions"),
          "3": () => setActivePanel("agents"),
          "4": () => setActivePanel("zones"),
          "5": () => setActivePanel("access"),
          "6": () => setActivePanel("payments"),
          "7": () => setActivePanel("search"),
          "8": () => setActivePanel("workflows"),
          "9": () => setActivePanel("infrastructure"),
          "0": () => setActivePanel("console"),
          "ctrl+i": toggleIdentitySwitcher,
          "z": () => toggleZoom(activePanel),
          "?": () => setHelpOpen(true),
          "q": shutdown,
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
