/**
 * Vertical sidebar navigation replacing the horizontal TabBar.
 *
 * Features:
 * - 12 panels with keyboard shortcuts
 * - 3-state indicators: active (bold + ◂), loading (spinner), error (red ●)
 * - 3 responsive breakpoints: full (>=120), collapsed (80-119), hidden (<80)
 * - Ctrl+B toggles visibility; hidden during zoom
 *
 * @see Issue #3497
 */

import React, { useState, useEffect } from "react";
import { useTerminalDimensions } from "@opentui/react";
import { palette } from "../theme.js";
import { NAV_ITEMS, type NavItem } from "../nav-items.js";
import { getSideNavMode, type SideNavMode } from "./side-nav-utils.js";
import type { PanelId } from "../../stores/global-store.js";

// Per-panel store imports for indicator selectors (Decision 1A)
import { useFilesStore } from "../../stores/files-store.js";
import { useVersionsStore } from "../../stores/versions-store.js";
import { useAgentsStore } from "../../stores/agents-store.js";
import { useZonesStore } from "../../stores/zones-store.js";
import { useAccessStore } from "../../stores/access-store.js";
import { usePaymentsStore } from "../../stores/payments-store.js";
import { useSearchStore } from "../../stores/search-store.js";
import { useWorkflowsStore } from "../../stores/workflows-store.js";
import { useInfraStore } from "../../stores/infra-store.js";
import { useApiConsoleStore } from "../../stores/api-console-store.js";
import { useConnectorsStore } from "../../stores/connectors-store.js";
import { useStackStore } from "../../stores/stack-store.js";

// =============================================================================
// Spinner frames (same as shared Spinner component)
// =============================================================================

const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const SPINNER_INTERVAL_MS = 80;

// =============================================================================
// Per-panel indicator hook (Decision 4A: individual primitive selectors)
// =============================================================================

interface PanelIndicatorMap {
  loading: Readonly<Record<PanelId, boolean>>;
  error: Readonly<Record<PanelId, boolean>>;
}

/**
 * Subscribes to per-panel loading and error state using individual primitive
 * selectors. Each selector returns a boolean, so Zustand's Object.is check
 * ensures re-renders only fire when the value actually changes.
 */
function usePanelIndicators(): PanelIndicatorMap {
  // Loading: only 3 stores expose top-level isLoading
  const versionsLoading = useVersionsStore((s) => s.isLoading);
  const zonesLoading = useZonesStore((s) => s.isLoading);
  const consoleLoading = useApiConsoleStore((s) => s.isLoading);

  // Error: most stores expose top-level error: string | null
  const filesError = useFilesStore((s) => !!s.error);
  const versionsError = useVersionsStore((s) => !!s.error);
  const agentsError = useAgentsStore((s) => !!s.error);
  const zonesError = useZonesStore((s) => !!s.error);
  const accessError = useAccessStore((s) => !!s.error);
  const paymentsError = usePaymentsStore((s) => !!s.error);
  const searchError = useSearchStore((s) => !!s.error);
  const workflowsError = useWorkflowsStore((s) => !!s.error);
  const infraError = useInfraStore((s) => !!s.error);
  const connectorsError = useConnectorsStore((s) => !!s.error);
  const stackError = useStackStore((s) => !!s.error);

  return {
    loading: {
      files: false,
      versions: versionsLoading,
      agents: false,
      zones: zonesLoading,
      access: false,
      payments: false,
      search: false,
      workflows: false,
      infrastructure: false,
      console: consoleLoading,
      connectors: false,
      stack: false,
    },
    error: {
      files: filesError,
      versions: versionsError,
      agents: agentsError,
      zones: zonesError,
      access: accessError,
      payments: paymentsError,
      search: searchError,
      workflows: workflowsError,
      infrastructure: infraError,
      console: false, // error is inside ResponseState, not panel-level
      connectors: connectorsError,
      stack: stackError,
    },
  };
}

// =============================================================================
// Component
// =============================================================================

interface SideNavProps {
  readonly activePanel: PanelId;
  readonly visible: boolean;
}

export function SideNav({ activePanel, visible }: SideNavProps): React.ReactNode {
  const { width: columns } = useTerminalDimensions();
  const mode = getSideNavMode(columns);

  const indicators = usePanelIndicators();

  // Spinner animation for loading indicators
  const [spinnerFrame, setSpinnerFrame] = useState(0);
  const hasAnyLoading = Object.values(indicators.loading).some(Boolean);

  useEffect(() => {
    if (!hasAnyLoading) return;
    const timer = setInterval(() => {
      setSpinnerFrame((prev) => (prev + 1) % SPINNER_FRAMES.length);
    }, SPINNER_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [hasAnyLoading]);

  if (!visible || mode === "hidden") return null;

  return (
    <box
      flexDirection="column"
      width={mode === "full" ? 18 : 6}
      height="100%"
      borderRight
      borderColor={palette.faint}
    >
      {NAV_ITEMS.map((item) => (
        <SideNavItem
          key={item.id}
          item={item}
          isActive={item.id === activePanel}
          isLoading={indicators.loading[item.id]}
          hasError={indicators.error[item.id]}
          mode={mode}
          spinnerFrame={SPINNER_FRAMES[spinnerFrame]!}
        />
      ))}
    </box>
  );
}

// =============================================================================
// Individual nav item (not memo'd per Decision 4A — 12 text lines is trivial)
// =============================================================================

interface SideNavItemProps {
  readonly item: NavItem;
  readonly isActive: boolean;
  readonly isLoading: boolean;
  readonly hasError: boolean;
  readonly mode: SideNavMode;
  readonly spinnerFrame: string;
}

function SideNavItem({
  item,
  isActive,
  isLoading,
  hasError,
  mode,
  spinnerFrame,
}: SideNavItemProps): React.ReactNode {
  // Determine the status indicator character
  const indicator = isLoading
    ? spinnerFrame
    : hasError
      ? "●"
      : isActive
        ? "◂"
        : " ";

  const indicatorColor = isLoading
    ? palette.accent
    : hasError
      ? palette.error
      : isActive
        ? palette.accent
        : undefined;

  if (mode === "collapsed") {
    // Collapsed: " ◎2◂" — icon + shortcut + indicator
    return (
      <box height={1}>
        <text>
          <span foregroundColor={isActive ? palette.accent : palette.muted}>
            {` ${item.icon}${item.shortcut}`}
          </span>
          <span foregroundColor={indicatorColor}>{indicator}</span>
        </text>
      </box>
    );
  }

  // Full: " 2:Versions  ◂" — shortcut:label + indicator
  const label = item.fullLabel;
  // Pad label to fill the available width: 18 total - 2 (left " ") - 2 (shortcut + ":") - 1 (indicator) - 1 (right pad) = 12 chars for label
  const paddedLabel = label.padEnd(12);

  return (
    <box height={1}>
      <text>
        {isActive ? (
          <>
            <span foregroundColor={palette.accent} bold>{` ${item.shortcut}:`}</span>
            <span foregroundColor={palette.accent} bold>{paddedLabel}</span>
          </>
        ) : (
          <>
            <span foregroundColor={palette.muted}>{` ${item.shortcut}:`}</span>
            <span foregroundColor={palette.muted}>{paddedLabel}</span>
          </>
        )}
        <span foregroundColor={indicatorColor}>{indicator}</span>
      </text>
    </box>
  );
}
