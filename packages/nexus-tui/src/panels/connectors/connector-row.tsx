/**
 * Shared connector row component used by Available and Mounted tabs.
 *
 * Renders a single row: selection prefix, connector name, provider, auth status,
 * mount path, and sync status. Props control column visibility per tab.
 */

import React from "react";
import { statusColor } from "../../shared/theme.js";

// =============================================================================
// Auth status indicator
// =============================================================================

const AUTH_INDICATORS: Record<string, { icon: string; color: string }> = {
  authed: { icon: "●", color: statusColor.healthy },
  expired: { icon: "●", color: statusColor.warning },
  no_auth: { icon: "○", color: statusColor.dim },
  unknown: { icon: "?", color: statusColor.dim },
  error: { icon: "✕", color: statusColor.error },
};

// =============================================================================
// Sync status indicator
// =============================================================================

const SYNC_INDICATORS: Record<string, { label: string; color: string }> = {
  synced: { label: "synced", color: statusColor.healthy },
  syncing: { label: "syncing", color: statusColor.warning },
  error: { label: "error", color: statusColor.error },
};

// =============================================================================
// Props
// =============================================================================

export interface ConnectorRowProps {
  /** Connector display name */
  readonly name: string;
  /** Category/provider group (e.g., "gws", "gh") */
  readonly category: string;
  /** Auth status string */
  readonly authStatus: string;
  /** Mount path or null if not mounted */
  readonly mountPath: string | null;
  /** Sync status or null */
  readonly syncStatus: string | null;
  /** Whether this row is selected */
  readonly selected: boolean;
  /** Whether to show the auth status column */
  readonly showAuth?: boolean;
  /** Whether to show the sync status column */
  readonly showSync?: boolean;
  /** Whether connector is currently being synced */
  readonly isSyncing?: boolean;
}

// =============================================================================
// Component
// =============================================================================

export function ConnectorRow({
  name,
  category,
  authStatus,
  mountPath,
  syncStatus,
  selected,
  showAuth = true,
  showSync = true,
  isSyncing = false,
}: ConnectorRowProps): React.ReactNode {
  const prefix = selected ? "▶ " : "  ";
  const auth = AUTH_INDICATORS[authStatus] ?? AUTH_INDICATORS.unknown;
  const displayName = name.replace(/_connector$/, "");
  const categoryLabel = category ? ` (${category})` : "";
  const mountLabel = mountPath ?? "—";

  // Build sync label
  let syncLabel = "";
  let syncColor = statusColor.dim;
  if (isSyncing) {
    syncLabel = "syncing…";
    syncColor = statusColor.warning;
  } else if (syncStatus) {
    const indicator = SYNC_INDICATORS[syncStatus];
    syncLabel = indicator?.label ?? syncStatus;
    syncColor = indicator?.color ?? statusColor.dim;
  }

  return (
    <box height={1} width="100%">
      <text>
        <span foregroundColor={selected ? statusColor.info : undefined}>
          {prefix}
        </span>
        <span bold={selected}>
          {displayName}
        </span>
        <span foregroundColor={statusColor.dim}>
          {categoryLabel}
        </span>
        {showAuth && (
          <>
            <span>{"  "}</span>
            <span foregroundColor={auth.color}>{auth.icon}</span>
            <span foregroundColor={statusColor.dim}>{` ${authStatus}`}</span>
          </>
        )}
        <span>{"  "}</span>
        <span foregroundColor={statusColor.reference}>{mountLabel}</span>
        {showSync && syncLabel && (
          <>
            <span>{"  "}</span>
            <span foregroundColor={syncColor}>{`[${syncLabel}]`}</span>
          </>
        )}
      </text>
    </box>
  );
}
