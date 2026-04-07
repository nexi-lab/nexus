/**
 * Root application component.
 *
 * Lazy-loads panels on first navigation for fast startup.
 * Shows PreConnectionScreen when the server is unavailable (Decision 3A).
 */

import { createEffect, createMemo, createSignal, Match, Show, Switch } from "solid-js";
import type { JSX } from "solid-js";
import { useGlobalStore, type PanelId } from "./stores/global-store.js";
import { useUiStore } from "./stores/ui-store.js";
import { useErrorStore } from "./stores/error-store.js";
import { useAnnouncementStore } from "./stores/announcement-store.js";
import { SideNav } from "./shared/components/side-nav.js";
import { StatusBar } from "./shared/components/status-bar.js";
import { ErrorBar } from "./shared/components/error-bar.js";
import { AnnouncementBar } from "./shared/components/announcement-bar.js";
import { Spinner } from "./shared/components/spinner.js";
import { useKeyboard } from "./shared/hooks/use-keyboard.js";
import type { KeyBindings } from "./shared/hooks/use-keyboard.js";
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
import { resetTerminal } from "./utils/terminal.js";
import { PANEL_DESCRIPTORS } from "./shared/navigation.js";
import {
  formatConnectionAnnouncement,
  formatErrorAnnouncement,
  formatPanelAnnouncement,
} from "./shared/accessibility-announcements.js";

// Lazy-loaded panels
// Direct imports — lazy() + Suspense prevents <Show keyed> from re-mounting panels.
import FileExplorerPanel from "./panels/files/file-explorer-panel.js";
import VersionsPanel from "./panels/versions/versions-panel.js";
import AgentsPanel from "./panels/agents/agents-panel.js";
import ZonesPanel from "./panels/zones/zones-panel.js";
import AccessPanel from "./panels/access/access-panel.js";
import PaymentsPanel from "./panels/payments/payments-panel.js";
import SearchPanel from "./panels/search/search-panel.js";
import WorkflowsPanel from "./panels/workflows/workflows-panel.js";
import EventsPanel from "./panels/events/events-panel.js";
import ApiConsolePanel from "./panels/api-console/api-console-panel.js";
import ConnectorsPanel from "./panels/connectors/connectors-panel.js";
import StackPanel from "./panels/stack/stack-panel.js";

/**
 * Exhaustive panel route map — adding a new PanelId without a matching entry
 * here is a compile-time error (Record<PanelId, ...> enforces completeness).
 */
export const PANEL_ROUTES: Record<PanelId, () => unknown> = {
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

// PanelRouter reads activePanel directly from the SolidJS-backed store.
// Since create-store uses solid-js/store under the hood, props.panel is
// reactive through babel-preset-solid's compiled getters — no polling needed.
function PanelRouter(props: { panel: PanelId }) {
  return (
    <Switch fallback={<text>Loading panel...</text>}>
      <Match when={props.panel === "files"}><FileExplorerPanel /></Match>
      <Match when={props.panel === "versions"}><VersionsPanel /></Match>
      <Match when={props.panel === "agents"}><AgentsPanel /></Match>
      <Match when={props.panel === "zones"}><ZonesPanel /></Match>
      <Match when={props.panel === "access"}><AccessPanel /></Match>
      <Match when={props.panel === "payments"}><PaymentsPanel /></Match>
      <Match when={props.panel === "search"}><SearchPanel /></Match>
      <Match when={props.panel === "workflows"}><WorkflowsPanel /></Match>
      <Match when={props.panel === "infrastructure"}><EventsPanel /></Match>
      <Match when={props.panel === "console"}><ApiConsolePanel /></Match>
      <Match when={props.panel === "connectors"}><ConnectorsPanel /></Match>
      <Match when={props.panel === "stack"}><StackPanel /></Match>
    </Switch>
  );
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
  resetTerminal();
  process.exit(0);
}

export function App() {
  const activePanel = useGlobalStore((s) => s.activePanel);
  const setActivePanel = useGlobalStore((s) => s.setActivePanel);

  const connectionStatus = useGlobalStore((s) => s.connectionStatus);
  const connectionError = useGlobalStore((s) => s.connectionError);
  const config = useGlobalStore((s) => s.config);
  const latestError = useErrorStore((s) => (s.errors.length > 0 ? s.errors[s.errors.length - 1] : null));
  const announce = useAnnouncementStore((s) => s.announce);
  const toggleZoom = useUiStore((s) => s.toggleZoom);
  const zoomedPanel = useUiStore((s) => s.zoomedPanel);
  const [identitySwitcherOpen, setIdentitySwitcherOpen] = createSignal(false);
  const [helpOpen, setHelpOpen] = createSignal(false);
  const [commandPaletteOpen, setCommandPaletteOpen] = createSignal(false);
  const [sideNavVisible, setSideNavVisible] = createSignal(true);
  const visibleTabs = useVisibleTabs(NAV_ITEMS);
  const { isFresh } = useFreshServer();
  const [welcomeDismissed, setWelcomeDismissed] = createSignal(false);
  const showWelcome = () => isFresh === true && !welcomeDismissed();

  // Determine if we should show the pre-connection screen (Decision 3A).
  // Must be a createMemo so the Show in JSX re-evaluates when connectionStatus changes.
  const showPreConnection = createMemo(() => {
    const cs = useGlobalStore((s) => s.connectionStatus);
    const ce = useGlobalStore((s) => s.connectionError);
    const cfg = useGlobalStore((s) => s.config);
    return detectConnectionState(cs, ce, cfg) !== "ready";
  });
  let previousPanel: PanelId | null = null;
  let previousConnection = connectionStatus;
  let lastErrorId: string | null = null;
  const panelLabel = createMemo(() => PANEL_DESCRIPTORS[useGlobalStore((s) => s.activePanel)]?.breadcrumbLabel ?? useGlobalStore((s) => s.activePanel));

  const setOverlayActive = useUiStore((s) => s.setOverlayActive);
  createEffect(() => {
    setOverlayActive(identitySwitcherOpen() || helpOpen() || commandPaletteOpen() || showWelcome());
  });

  createEffect(() => {
    const visibleIds = visibleTabs.map((tab) => tab.id);
    const ap = useGlobalStore((s) => s.activePanel);
    if (visibleIds.length > 0 && !visibleIds.includes(ap)) {
      setActivePanel(visibleIds[0]!);
    }
  });

  createEffect(() => {
    const ap = useGlobalStore((s) => s.activePanel);
    if (previousPanel !== null && previousPanel !== ap) {
      announce(formatPanelAnnouncement(panelLabel()));
    }
    previousPanel = ap;
  });

  createEffect(() => {
    const cs = useGlobalStore((s) => s.connectionStatus);
    const ce = useGlobalStore((s) => s.connectionError);
    if (previousConnection !== cs) {
      announce(
        formatConnectionAnnouncement(cs, ce),
        cs === "error" ? "error" : cs === "connected" ? "success" : "info",
      );
    }
    previousConnection = cs;
  });

  createEffect(() => {
    const err = useErrorStore((s) => (s.errors.length > 0 ? s.errors[s.errors.length - 1] : null));
    if (!err || lastErrorId === err.id) return;
    lastErrorId = err.id;
    const cs = useGlobalStore((s) => s.connectionStatus);
    const ce = useGlobalStore((s) => s.connectionError);
    if (
      err.source === "global"
      && cs === "error"
      && err.message === ce
    ) {
      return;
    }
    announce(formatErrorAnnouncement(err.message), "error");
  });

  // Centralized panel navigation — rejects panels not in the current visible set
  // so that disabled-brick panels cannot be entered via keyboard shortcuts.
  const navigateToPanel = (panelId: PanelId): void => {
    if (useUiStore.getState().fileEditorOpen) return;
    if (visibleTabs.some((t) => t.id === panelId)) {
      setActivePanel(panelId);
    }
  };

  const toggleIdentitySwitcher = () => {
    setIdentitySwitcherOpen((prev) => !prev);
  };

  const closeIdentitySwitcher = () => {
    setIdentitySwitcherOpen(false);
  };

  const closeCommandPalette = () => {
    setCommandPaletteOpen(false);
  };

  const commandPaletteItems = createMemo<readonly CommandPaletteItem[]>(() => {
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
  });

  const keyBindings = createMemo((): KeyBindings => {
    if (showPreConnection()) {
      return {
          // Pre-connection screen handles its own keybindings
          "q": shutdown,
        };
    }

    if (identitySwitcherOpen() || helpOpen() || commandPaletteOpen() || showWelcome()) {
      return {
          // When an overlay is open, only dismiss keys work from app level.
          "ctrl+i": toggleIdentitySwitcher,
        };
    }

    return {
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
          "z": () => { if (!useUiStore.getState().fileEditorOpen) toggleZoom(useGlobalStore.getState().activePanel); },
          "?": () => { if (!useUiStore.getState().fileEditorOpen) setHelpOpen(true); },
          "q": () => { if (!useUiStore.getState().fileEditorOpen) shutdown(); },
        };
  });

  useKeyboard(keyBindings);

  // Render both main UI and pre-connection screen. Use <Show> to toggle
  // visibility, NOT to conditionally mount/unmount. This ensures the main
  // UI's reactive scope (signals, effects, intervals) is never disposed.
  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Pre-connection overlay — shown on top when not connected */}
      <Show when={showPreConnection()}>
        <PreConnectionScreen />
        <StatusBar />
      </Show>

      {/* Main UI — always rendered, hidden when pre-connection is shown */}
      <Show when={!showPreConnection()}>
        {/* Main row: sidebar + panel content */}
        <box flexGrow={1} flexDirection="row">
          <SideNav activePanel={useGlobalStore((s) => s.activePanel)} visible={sideNavVisible() && !useUiStore((s) => s.zoomedPanel)} onSelect={setActivePanel} />
          <box flexGrow={1}>
            <PanelRouter panel={useGlobalStore((s) => s.activePanel) as PanelId} />
          </box>
        </box>

        {/* Overlays */}
        {showWelcome() && <WelcomeScreen onDismiss={() => setWelcomeDismissed(true)} />}
        <IdentitySwitcher visible={identitySwitcherOpen()} onClose={closeIdentitySwitcher} />
        <CommandPalette visible={commandPaletteOpen()} commands={commandPaletteItems()} onClose={closeCommandPalette} />
        <AppConfirmDialog />
        <HelpOverlay visible={helpOpen()} panel={useGlobalStore((s) => s.activePanel)} onDismiss={() => setHelpOpen(false)} />

        {/* Error bar + Status bar */}
        <AnnouncementBar />
        <ErrorBar />
        <StatusBar />
      </Show>
    </box>
  );
}
