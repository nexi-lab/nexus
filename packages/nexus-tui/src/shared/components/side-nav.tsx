/**
 * Vertical sidebar navigation replacing the horizontal TabBar.
 *
 * Features:
 * - 12 panels with keyboard shortcuts
 * - 6-state indicators: active (bold + ◂), loading (spinner), error (red ●),
 *   unseen (blue ●), stale (dimmed text), healthy (no indicator)
 * - 3 responsive breakpoints: full (>=120), collapsed (80-119), hidden (<80)
 * - Ctrl+B toggles visibility; hidden during zoom
 *
 * @see Issue #3497, #3503
 */

import React, { useState, useEffect } from "react";
import { useTerminalDimensions } from "@opentui/react";
import { palette } from "../theme.js";
import { NAV_ITEMS, type NavItem } from "../nav-items.js";
import { getSideNavMode, STALE_THRESHOLD_MS, type SideNavMode } from "./side-nav-utils.js";
import type { PanelId } from "../../stores/global-store.js";
import { useUiStore } from "../../stores/ui-store.js";
import { useVisibleTabs } from "../hooks/use-visible-tabs.js";

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
  unseen: Readonly<Record<PanelId, boolean>>;
  stale: Readonly<Record<PanelId, boolean>>;
}

/**
 * Subscribes to per-panel loading and error state using individual primitive
 * selectors. Each selector returns a boolean, so Zustand's Object.is check
 * ensures re-renders only fire when the value actually changes.
 *
 * Also derives unseen (new data since last visit) and stale (data not
 * refreshed within STALE_THRESHOLD_MS) from centralized ui-store timestamps.
 */
function usePanelIndicators(now: number): PanelIndicatorMap {
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

  // Timestamps for unseen/stale derivation
  const dataTs = useUiStore((s) => s.panelDataTimestamps);
  const visitTs = useUiStore((s) => s.panelVisitTimestamps);

  // Derive unseen and stale per panel
  const unseen = {} as Record<PanelId, boolean>;
  const stale = {} as Record<PanelId, boolean>;
  for (const item of NAV_ITEMS) {
    const lastData = dataTs[item.id] ?? 0;
    const lastVisit = visitTs[item.id] ?? 0;
    unseen[item.id] = lastData > 0 && lastVisit < lastData;
    stale[item.id] = lastData > 0 && now - lastData > STALE_THRESHOLD_MS;
  }

  return {
    loading: {
      files: false,           // TODO: wire when files-store adds top-level isLoading
      versions: versionsLoading,
      agents: false,          // TODO: wire when agents-store adds top-level isLoading
      zones: zonesLoading,
      access: false,          // TODO: wire when access-store adds top-level isLoading
      payments: false,        // TODO: wire when payments-store adds top-level isLoading
      search: false,          // TODO: wire when search-store adds top-level isLoading
      workflows: false,       // TODO: wire when workflows-store adds top-level isLoading
      infrastructure: false,  // TODO: wire when infra-store adds top-level isLoading
      console: consoleLoading,
      connectors: false,      // TODO: wire when connectors-store adds top-level isLoading
      stack: false,           // TODO: wire when stack-store adds top-level isLoading
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
    unseen,
    stale,
  };
}

// =============================================================================
// Component
// =============================================================================

interface SideNavProps {
  readonly activePanel: PanelId;
  readonly visible: boolean;
  readonly onSelect?: (id: PanelId) => void;
}

/** Interval (ms) for re-evaluating stale state. */
const STALE_CHECK_INTERVAL_MS = 10_000;

export function SideNav({ activePanel, visible, onSelect }: SideNavProps): React.ReactNode {
  const { width: columns } = useTerminalDimensions();
  const mode = getSideNavMode(columns);

  // Periodic tick so stale derivation re-evaluates over time
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), STALE_CHECK_INTERVAL_MS);
    return () => clearInterval(timer);
  }, []);

  const indicators = usePanelIndicators(now);

  // Apply same brick filtering as the command palette so disabled panels
  // are not advertised in the primary navigation.
  const visibleItems = useVisibleTabs(NAV_ITEMS);

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
      {visibleItems.map((item) => (
        <box key={item.id} height={1} onMouseDown={() => onSelect?.(item.id as PanelId)}>
          <SideNavItem
            item={item as NavItem}
            isActive={item.id === activePanel}
            isLoading={indicators.loading[item.id]}
            hasError={indicators.error[item.id]}
            isUnseen={indicators.unseen[item.id]}
            isStale={indicators.stale[item.id]}
            mode={mode}
            spinnerFrame={SPINNER_FRAMES[spinnerFrame]!}
          />
        </box>
      ))}
    </box>
  );
}

// =============================================================================
// Individual nav item (not memo'd per Decision 4A — 12 text lines is trivial)
// =============================================================================

/** Blue accent for unseen indicators. */
const UNSEEN_COLOR = "#60A5FA";

interface SideNavItemProps {
  readonly item: NavItem;
  readonly isActive: boolean;
  readonly isLoading: boolean;
  readonly hasError: boolean;
  readonly isUnseen: boolean;
  readonly isStale: boolean;
  readonly mode: SideNavMode;
  readonly spinnerFrame: string;
}

function SideNavItem({
  item,
  isActive,
  isLoading,
  hasError,
  isUnseen,
  isStale,
  mode,
  spinnerFrame,
}: SideNavItemProps): React.ReactNode {
  // Determine the status indicator character
  // Priority: loading > error > unseen (when not active) > active > healthy
  const indicator = isLoading
    ? spinnerFrame
    : hasError
      ? "●"
      : isUnseen && !isActive
        ? "●"
        : isActive
          ? "◂"
          : " ";

  const indicatorColor = isLoading
    ? palette.accent
    : hasError
      ? palette.error
      : isUnseen && !isActive
        ? UNSEEN_COLOR
        : isActive
          ? palette.accent
          : undefined;

  // Text color: active > stale (dimmed) > normal muted
  const textColor = isActive
    ? palette.accent
    : isStale && !isUnseen
      ? palette.faint
      : palette.muted;

  if (mode === "collapsed") {
    // Collapsed: " ◎2◂" — icon + shortcut + indicator
    return (
      <box height={1}>
        <text>
          <span foregroundColor={isActive ? palette.accent : textColor}>
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
            <span foregroundColor={textColor}>{` ${item.shortcut}:`}</span>
            <span foregroundColor={textColor}>{paddedLabel}</span>
          </>
        )}
        <span foregroundColor={indicatorColor}>{indicator}</span>
      </text>
    </box>
  );
}
