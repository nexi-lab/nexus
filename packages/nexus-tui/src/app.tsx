/**
 * Root application component.
 *
 * Lazy-loads panels on first navigation for fast startup.
 */

import React, { lazy, Suspense } from "react";
import { useGlobalStore, type PanelId } from "./stores/global-store.js";
import { TabBar, type Tab } from "./shared/components/tab-bar.js";
import { StatusBar } from "./shared/components/status-bar.js";
import { ErrorBoundary } from "./shared/components/error-boundary.js";
import { Spinner } from "./shared/components/spinner.js";
import { useKeyboard } from "./shared/hooks/use-keyboard.js";

// Lazy-loaded panels
const FileExplorerPanel = lazy(() => import("./panels/files/file-explorer-panel.js"));
const ApiConsolePanel = lazy(() => import("./panels/api-console/api-console-panel.js"));
const EventsPanel = lazy(() => import("./panels/events/events-panel.js"));

const TABS: readonly Tab[] = [
  { id: "files", label: "Files", shortcut: "1" },
  { id: "console", label: "Console", shortcut: "2" },
  { id: "infrastructure", label: "Events", shortcut: "3" },
];

function PanelRouter(): React.ReactNode {
  const activePanel = useGlobalStore((s) => s.activePanel);

  switch (activePanel) {
    case "files":
      return <FileExplorerPanel />;
    case "console":
      return <ApiConsolePanel />;
    case "infrastructure":
      return <EventsPanel />;
    default:
      return (
        <box height="100%" width="100%" justifyContent="center" alignItems="center">
          <text>{`Panel "${activePanel}" — coming in Phase 2+`}</text>
        </box>
      );
  }
}

export function App(): React.ReactNode {
  const activePanel = useGlobalStore((s) => s.activePanel);
  const setActivePanel = useGlobalStore((s) => s.setActivePanel);

  useKeyboard({
    "1": () => setActivePanel("files"),
    "2": () => setActivePanel("console"),
    "3": () => setActivePanel("infrastructure"),
    "q": () => process.exit(0),
  });

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

      {/* Status bar */}
      <StatusBar />
    </box>
  );
}
