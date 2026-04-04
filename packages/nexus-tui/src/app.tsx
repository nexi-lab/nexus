/**
 * Root application component.
 *
 * Lazy-loads panels on first navigation for fast startup.
 * Shows PreConnectionScreen when the server is unavailable (Decision 3A).
 */

import React, { lazy, Suspense, useState, useCallback, useEffect, useRef } from "react";
import { useGlobalStore, type PanelId } from "./stores/global-store.js";
import { useUiStore } from "./stores/ui-store.js";
import { useErrorStore } from "./stores/error-store.js";
import { useAnnouncementStore } from "./stores/announcement-store.js";
import { SideNav } from "./shared/components/side-nav.js";
import { StatusBar } from "./shared/components/status-bar.js";
import { ErrorBar } from "./shared/components/error-bar.js";
import { AnnouncementBar } from "./shared/components/announcement-bar.js";
import { ErrorBoundary } from "./shared/components/error-boundary.js";
import { Spinner } from "./shared/components/spinner.js";
import { useKeyboard } from "./shared/hooks/use-keyboard.js";
import { IdentitySwitcher } from "./shared/components/identity-switcher.js";
import { AppConfirmDialog } from "./shared/components/app-confirm-dialog.js";
import { HelpOverlay } from "./shared/components/help-overlay.js";
import { WelcomeScreen } from "./shared/components/welcome-screen.js";
import { PreConnectionScreen } from "./shared/components/pre-connection-screen.js";
import { CommandPalette } from "./shared/components/command-palette.js";
import { type CommandPaletteItem } from "./shared/command-palette.js";
import { useFreshServer } from "./shared/hooks/use-fresh-server.js";
import { detectConnectionState } from "./shared/hooks/use-connection-state.js";
import { useVisibleTabs } from "./shared/hooks/use-visible-tabs.js";
import { NAV_ITEMS } from "./shared/nav-items.js";
import { killAllProcesses } from "./services/command-runner.js";
import { PANEL_DESCRIPTORS } from "./shared/navigation.js";
import {
  formatConnectionAnnouncement,
  formatErrorAnnouncement,
  formatPanelAnnouncement,
} from "./shared/accessibility-announcements.js";

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
const StackPanel = lazy(() => import("./panels/stack/stack-panel.js"));

/**
 * Exhaustive panel route map — adding a new PanelId without a matching entry
 * here is a compile-time error (Record<PanelId, ...> enforces completeness).
 */
export const PANEL_ROUTES: Record<PanelId, () => React.ReactNode> = {
  files:          () => <FileExplorerPanel />,
  versions:       () => <VersionsPanel />,
  agents:         () => <AgentsPanel />,
  zones:          () => <ZonesPanel />,
  access:         () => <AccessPanel />,
  payments:       () => <PaymentsPanel />,
  search:         () => <SearchPanel />,
  workflows:      () => <WorkflowsPanel />,
  infrastructure: () => <EventsPanel />,
  console:        () => <ApiConsolePanel />,
  connectors:     () => <ConnectorsPanel />,
  stack:          () => <StackPanel />,
};

function PanelRouter(): React.ReactNode {
  const activePanel = useGlobalStore((s) => s.activePanel);
  return PANEL_ROUTES[activePanel]();
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
  const latestError = useErrorStore((s) => (s.errors.length > 0 ? s.errors[s.errors.length - 1] : null));
  const announce = useAnnouncementStore((s) => s.announce);
  const toggleZoom = useUiStore((s) => s.toggleZoom);
  const zoomedPanel = useUiStore((s) => s.zoomedPanel);
  const [identitySwitcherOpen, setIdentitySwitcherOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  const [sideNavVisible, setSideNavVisible] = useState(true);
  const visibleTabs = useVisibleTabs(NAV_ITEMS);
  const { isFresh } = useFreshServer();
  const [welcomeDismissed, setWelcomeDismissed] = useState(false);
  const showWelcome = isFresh === true && !welcomeDismissed;

  // Determine if we should show the pre-connection screen (Decision 3A)
  // Only hide it when fully connected — "connecting" still shows the pre-connection
  // screen with a spinner to avoid flashing the main UI during connection attempts.
  const connState = detectConnectionState(connectionStatus, connectionError, config);
  const showPreConnection = connState !== "ready";
  const previousPanelRef = useRef<PanelId | null>(null);
  const previousConnectionRef = useRef(connectionStatus);
  const lastErrorIdRef = useRef<string | null>(null);
  const panelLabel = PANEL_DESCRIPTORS[activePanel]?.breadcrumbLabel ?? activePanel;

  const setOverlayActive = useUiStore((s) => s.setOverlayActive);
  useEffect(() => {
    setOverlayActive(identitySwitcherOpen || helpOpen || commandPaletteOpen || showWelcome);
  }, [identitySwitcherOpen, helpOpen, commandPaletteOpen, showWelcome, setOverlayActive]);

  useEffect(() => {
    const visibleIds = visibleTabs.map((tab) => tab.id);
    if (visibleIds.length > 0 && !visibleIds.includes(activePanel)) {
      setActivePanel(visibleIds[0]!);
    }
  }, [activePanel, setActivePanel, visibleTabs]);

  useEffect(() => {
    if (previousPanelRef.current !== null && previousPanelRef.current !== activePanel) {
      announce(formatPanelAnnouncement(panelLabel));
    }
    previousPanelRef.current = activePanel;
  }, [activePanel, panelLabel, announce]);

  useEffect(() => {
    if (previousConnectionRef.current !== connectionStatus) {
      announce(
        formatConnectionAnnouncement(connectionStatus, connectionError),
        connectionStatus === "error" ? "error" : connectionStatus === "connected" ? "success" : "info",
      );
    }
    previousConnectionRef.current = connectionStatus;
  }, [connectionStatus, connectionError, announce]);

  useEffect(() => {
    if (!latestError || lastErrorIdRef.current === latestError.id) return;
    lastErrorIdRef.current = latestError.id;
    if (
      latestError.source === "global"
      && connectionStatus === "error"
      && latestError.message === connectionError
    ) {
      return;
    }
    announce(formatErrorAnnouncement(latestError.message), "error");
  }, [latestError, connectionStatus, connectionError, announce]);

  // Centralized panel navigation — rejects panels not in the current visible set
  // so that disabled-brick panels cannot be entered via keyboard shortcuts.
  const navigateToPanel = useCallback((panelId: PanelId): void => {
    if (useUiStore.getState().fileEditorOpen) return;
    if (visibleTabs.some((t) => t.id === panelId)) {
      setActivePanel(panelId);
    }
  }, [visibleTabs, setActivePanel]);

  const toggleIdentitySwitcher = useCallback(() => {
    setIdentitySwitcherOpen((prev) => !prev);
  }, []);

  const closeIdentitySwitcher = useCallback(() => {
    setIdentitySwitcherOpen(false);
  }, []);

  const closeCommandPalette = useCallback(() => {
    setCommandPaletteOpen(false);
  }, []);

  const commandPaletteItems = React.useMemo<readonly CommandPaletteItem[]>(() => {
    // Only show panels that are currently brick-enabled so hidden panels
    // cannot be navigated to through the command palette.
    const visiblePanelIds = new Set(visibleTabs.map((t) => t.id));
    const panelCommands: CommandPaletteItem[] = NAV_ITEMS
      .filter((item) => visiblePanelIds.has(item.id))
      .map((item) => ({
        id: `panel:${item.id}`,
        title: `Switch to ${item.fullLabel}`,
        section: "Panels",
        hint: item.shortcut,
        keywords: [item.id, item.fullLabel, "panel", "switch"],
        run: () => setActivePanel(item.id),
      }));

    const appCommands: CommandPaletteItem[] = [
      {
        id: "app:help",
        title: "Open help overlay",
        section: "Global",
        hint: "?",
        keywords: ["help", "shortcuts", "bindings"],
        run: () => setHelpOpen(true),
      },
      {
        id: "app:identity",
        title: "Open identity switcher",
        section: "Global",
        hint: "Ctrl+I",
        keywords: ["identity", "agent", "subject", "zone"],
        run: () => setIdentitySwitcherOpen(true),
      },
      {
        id: "app:disconnect",
        title: "Disconnect and return to setup",
        section: "Global",
        hint: "Ctrl+D",
        keywords: ["disconnect", "setup", "reconnect"],
        run: () => useGlobalStore.getState().setConnectionStatus("error", "Disconnected by user"),
      },
      {
        id: "app:zoom",
        title: zoomedPanel === activePanel ? "Exit zoom" : `Zoom ${activePanel}`,
        section: "Global",
        hint: "z",
        keywords: ["zoom", "fullscreen", activePanel],
        run: () => toggleZoom(activePanel),
      },
      {
        id: "app:quit",
        title: "Quit Nexus TUI",
        section: "Global",
        hint: "q",
        keywords: ["quit", "exit", "close"],
        run: shutdown,
      },
    ];

    return [...appCommands, ...panelCommands];
  }, [visibleTabs, setActivePanel, zoomedPanel, activePanel, toggleZoom]);

  useKeyboard(
    showPreConnection
      ? {
          // Pre-connection screen handles its own keybindings
          "q": shutdown,
        }
      : identitySwitcherOpen || helpOpen || commandPaletteOpen || showWelcome
      ? {
          // When an overlay is open, only dismiss keys work from app level.
          "ctrl+i": toggleIdentitySwitcher,
        }
      : {
          // navigateToPanel checks both fileEditorOpen and brick visibility,
          // so disabled-brick panels cannot be entered via keyboard shortcuts.
          "1": () => navigateToPanel("files"),
          "2": () => navigateToPanel("versions"),
          "3": () => navigateToPanel("agents"),
          "4": () => navigateToPanel("zones"),
          "5": () => navigateToPanel("access"),
          "6": () => navigateToPanel("payments"),
          "7": () => navigateToPanel("search"),
          "8": () => navigateToPanel("workflows"),
          "9": () => navigateToPanel("infrastructure"),
          "0": () => navigateToPanel("console"),
          // Connectors and Stack have no dedicated global shortcuts: OpenTUI
          // broadcasts to all handlers simultaneously, so any uppercase letter
          // can collide with an existing panel-local binding (e.g. shift+c is
          // already claimed by Access/fraud for fetchCollusionRings). Both panels
          // remain reachable via the command palette (`:`) or the side nav.
          "ctrl+b": () => { setSideNavVisible((v) => !v); },
          "ctrl+p": () => { if (!useUiStore.getState().fileEditorOpen) setCommandPaletteOpen(true); },
          ":": () => { if (!useUiStore.getState().fileEditorOpen) setCommandPaletteOpen(true); },
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
      {/* Main row: sidebar + panel content */}
      <box flexGrow={1} flexDirection="row">
        {/* Side navigation (Ctrl+B toggles, hidden when zoomed) */}
        <SideNav activePanel={activePanel} visible={sideNavVisible && !zoomedPanel} />

        {/* Panel content */}
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
      </box>

      {/* Overlays */}
      {showWelcome && <WelcomeScreen onDismiss={() => setWelcomeDismissed(true)} />}
      <IdentitySwitcher visible={identitySwitcherOpen} onClose={closeIdentitySwitcher} />
      <CommandPalette visible={commandPaletteOpen} commands={commandPaletteItems} onClose={closeCommandPalette} />
      <AppConfirmDialog />
      <HelpOverlay visible={helpOpen} panel={activePanel} onDismiss={() => setHelpOpen(false)} />

      {/* Error bar + Status bar */}
      <AnnouncementBar />
      <ErrorBar />
      <StatusBar />
    </box>
  );
}
