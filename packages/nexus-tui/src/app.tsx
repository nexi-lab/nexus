/**
 * Root application component.
 *
 * Lazy-loads panels on first navigation for fast startup.
 */

import React, { lazy, Suspense, useState, useCallback } from "react";
import { useGlobalStore, type PanelId } from "./stores/global-store.js";
import { TabBar, type Tab } from "./shared/components/tab-bar.js";
import { StatusBar } from "./shared/components/status-bar.js";
import { ErrorBoundary } from "./shared/components/error-boundary.js";
import { Spinner } from "./shared/components/spinner.js";
import { useKeyboard } from "./shared/hooks/use-keyboard.js";
import { IdentitySwitcher } from "./shared/components/identity-switcher.js";

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

export function App(): React.ReactNode {
  const activePanel = useGlobalStore((s) => s.activePanel);
  const setActivePanel = useGlobalStore((s) => s.setActivePanel);
  const [identitySwitcherOpen, setIdentitySwitcherOpen] = useState(false);

  const toggleIdentitySwitcher = useCallback(() => {
    setIdentitySwitcherOpen((prev) => !prev);
  }, []);

  const closeIdentitySwitcher = useCallback(() => {
    setIdentitySwitcherOpen(false);
  }, []);

  useKeyboard(
    identitySwitcherOpen
      ? {
          // When the overlay is open, only Ctrl+I closes it from the app level.
          // All other keys are handled by IdentitySwitcher itself.
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
          "q": () => process.exit(0),
        },
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Tab bar */}
      <TabBar tabs={TABS} activeTab={activePanel} onSelect={(id) => setActivePanel(id as PanelId)} />

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

      {/* Identity switcher overlay */}
      <IdentitySwitcher visible={identitySwitcherOpen} onClose={closeIdentitySwitcher} />

      {/* Status bar */}
      <StatusBar />
    </box>
  );
}
