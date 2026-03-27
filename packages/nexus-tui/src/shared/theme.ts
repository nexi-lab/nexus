/**
 * Semantic color system for the TUI.
 *
 * All visual styling should reference these tokens rather than raw color names.
 * Colors use ANSI 16 names for universal terminal compatibility.
 *
 * @see Issue #3066, Phase A1
 */

// =============================================================================
// Semantic color tokens
// =============================================================================

/**
 * Status-based colors for state indicators across all panels.
 */
export const statusColor = {
  /** Healthy, active, committed, connected */
  healthy: "green",
  /** Completed successfully — same as healthy (terminal vs ongoing state) */
  success: "green",
  /** Warning, starting, stopping, stale */
  warning: "yellow",
  /** Error, failed, rolled_back, expired */
  error: "red",
  /** Info, informational highlights */
  info: "cyan",
  /** Focus, selected, active UI chrome */
  focus: "yellow",
  /** Secondary text, hints, timestamps */
  dim: "gray",
  /** Agent identity, delegation chains */
  identity: "magenta",
  /** File paths, URNs, zone IDs */
  reference: "blue",
} as const;

export type StatusColorKey = keyof typeof statusColor;

// =============================================================================
// Connection status → color mapping
// =============================================================================

export const connectionColor: Record<string, string> = {
  connected: statusColor.healthy,
  connecting: statusColor.warning,
  disconnected: statusColor.dim,
  error: statusColor.error,
};

// =============================================================================
// Brick state → color mapping
// =============================================================================

export const brickStateColor: Record<string, string> = {
  active: statusColor.healthy,
  registered: statusColor.info,
  starting: statusColor.warning,
  stopping: statusColor.warning,
  unmounted: statusColor.dim,
  unregistered: statusColor.dim,
  failed: statusColor.error,
};

// =============================================================================
// Transaction status → color mapping
// =============================================================================

export const transactionStatusColor: Record<string, string> = {
  active: statusColor.warning,
  committed: statusColor.healthy,
  rolled_back: statusColor.error,
  expired: statusColor.dim,
};

// =============================================================================
// HTTP status code → color mapping
// =============================================================================

export function httpStatusColor(status: number): string {
  if (status >= 200 && status < 300) return statusColor.healthy;
  if (status >= 400 && status < 500) return statusColor.warning;
  if (status >= 500) return statusColor.error;
  return statusColor.dim;
}

// =============================================================================
// Agent phase → color mapping
// =============================================================================

export const agentPhaseColor: Record<string, string> = {
  ready: statusColor.healthy,
  active: statusColor.healthy,
  warming: statusColor.warning,
  evicting: statusColor.warning,
  evicted: statusColor.dim,
  failed: statusColor.error,
};

// =============================================================================
// Agent state → color mapping (for agent list)
// =============================================================================

export const agentStateColor: Record<string, string> = {
  registered: statusColor.info,
  delegated: statusColor.identity,
  running: statusColor.healthy,
  connected: statusColor.healthy,
  disconnected: statusColor.dim,
};

// =============================================================================
// Delegation mode → color mapping
// =============================================================================

export const delegationModeColor: Record<string, string> = {
  shared: statusColor.info,
  copy: statusColor.warning,
  clean: statusColor.error,
};

// =============================================================================
// Delegation status → color mapping
// =============================================================================

export const delegationStatusColor: Record<string, string> = {
  active: statusColor.healthy,
  revoked: statusColor.error,
  expired: statusColor.dim,
  completed: statusColor.info,
};

// =============================================================================
// Focus / UI chrome colors
// =============================================================================

export const focusColor = {
  /** Active pane border */
  activeBorder: statusColor.focus,
  /** Inactive pane border */
  inactiveBorder: statusColor.dim,
  /** Selected item highlight */
  selected: statusColor.focus,
  /** Help bar action keys */
  actionKey: statusColor.focus,
  /** Help bar navigation keys */
  navKey: statusColor.dim,
} as const;

// =============================================================================
// Rich color palette — hex values for modern terminals
//
// Used by shared chrome components (tab bar, status bar, error bar).
// Components should prefer semantic tokens (statusColor, focusColor) for
// state-dependent colors and use palette for structural UI chrome.
// =============================================================================

export const palette = {
  /** Primary accent — active tabs, selected items, key bindings */
  accent: "#D4A017",
  /** Secondary accent — hover, secondary actions */
  accentDim: "#B8890F",
  /** Success — green confirmation */
  success: "#34D399",
  /** Error — bright red */
  error: "#F87171",
  /** Error secondary — softer red for hints */
  errorDim: "#FCA5A5",
  /** Warning — amber */
  warning: "#FBBF24",
  /** Muted text — labels, separators */
  muted: "#8B8B92",
  /** Very muted — borders, inactive chrome */
  faint: "#2A2A2E",
  /** Bright text — active content */
  bright: "#EDEDEF",
  /** Header/title text */
  title: "#EDEDEF",
} as const;
