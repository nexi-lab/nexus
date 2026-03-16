/**
 * Bottom status bar showing connection state, active identity, and path.
 *
 * Enhanced with semantic colors from theme.ts (Phase A1).
 */

import React from "react";
import { useGlobalStore } from "../../stores/global-store.js";
import { useEventsStore } from "../../stores/events-store.js";
import { connectionColor, statusColor } from "../theme.js";

const STATUS_ICONS: Record<string, string> = {
  connected: "●",
  connecting: "◐",
  disconnected: "○",
  error: "✗",
};

export function StatusBar(): React.ReactNode {
  const status = useGlobalStore((s) => s.connectionStatus);
  const config = useGlobalStore((s) => s.config);
  const serverVersion = useGlobalStore((s) => s.serverVersion);
  const zoneId = useGlobalStore((s) => s.zoneId);
  const activePanel = useGlobalStore((s) => s.activePanel);
  const userInfo = useGlobalStore((s) => s.userInfo);
  const enabledBricks = useGlobalStore((s) => s.enabledBricks);

  // Check if events panel has active filters
  const eventFilters = useEventsStore((s) => s.filters);
  const hasActiveFilter = eventFilters.eventType !== null || eventFilters.search !== null;

  const icon = STATUS_ICONS[status] ?? "?";
  const baseUrl = config.baseUrl ?? "localhost:2026";

  // Build identity segment
  const identityParts: string[] = [];
  if (userInfo?.display_name ?? userInfo?.username) {
    identityParts.push(userInfo!.display_name ?? userInfo!.username!);
  } else if (config.agentId) {
    identityParts.push(`agent:${config.agentId}`);
  }
  if (config.subject && config.subject !== config.agentId) {
    identityParts.push(`sub:${config.subject}`);
  }

  // Build zone segment
  const zone = config.zoneId ?? zoneId;

  return (
    <box
      height={1}
      width="100%"
      flexDirection="row"
    >
      <text>
        <text foregroundColor={connectionColor[status]}>{icon}</text>
        <text dimColor>{` ${status} │ `}</text>
        <text>{baseUrl}</text>
        {identityParts.length > 0 && (
          <text>
            <text dimColor>{" │ "}</text>
            <text foregroundColor={statusColor.identity}>{identityParts.join(", ")}</text>
          </text>
        )}
        {serverVersion && (
          <text>
            <text dimColor>{" │ "}</text>
            <text dimColor>{`v${serverVersion}`}</text>
          </text>
        )}
        {zone && (
          <text>
            <text dimColor>{" │ "}</text>
            <text foregroundColor={statusColor.reference}>{`zone:${zone}`}</text>
          </text>
        )}
        {enabledBricks.length > 0 && (
          <text>
            <text dimColor>{" │ "}</text>
            <text foregroundColor={statusColor.info}>{`${enabledBricks.length} bricks`}</text>
          </text>
        )}
        <text dimColor>{" │ "}</text>
        <text foregroundColor={statusColor.info}>{`[${activePanel}]`}</text>
        {hasActiveFilter && (
          <text foregroundColor="yellow">{" [filtered]"}</text>
        )}
      </text>
    </box>
  );
}
