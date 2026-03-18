/**
 * Bottom status bar showing connection state, active identity, and path.
 *
 * Enhanced with semantic colors from theme.ts (Phase A1).
 *
 * Note: OpenTUI does not support nested <text> elements. Use <span> for
 * inline styled segments inside a <text>.
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
        <span foregroundColor={connectionColor[status]}>{icon}</span>
        <span dimColor>{` ${status} │ `}</span>
        <span>{baseUrl}</span>
        {identityParts.length > 0 ? (
          <>
            <span dimColor>{" │ "}</span>
            <span foregroundColor={statusColor.identity}>{identityParts.join(", ")}</span>
          </>
        ) : ""}
        {serverVersion ? (
          <>
            <span dimColor>{" │ "}</span>
            <span dimColor>{`v${serverVersion}`}</span>
          </>
        ) : ""}
        {zone ? (
          <>
            <span dimColor>{" │ "}</span>
            <span foregroundColor={statusColor.reference}>{`zone:${zone}`}</span>
          </>
        ) : ""}
        {enabledBricks.length > 0 ? (
          <>
            <span dimColor>{" │ "}</span>
            <span foregroundColor={statusColor.info}>{`${enabledBricks.length} bricks`}</span>
          </>
        ) : ""}
        <span dimColor>{" │ "}</span>
        <span foregroundColor={statusColor.info}>{`[${activePanel}]`}</span>
        {hasActiveFilter ? (
          <span foregroundColor="yellow">{" [filtered]"}</span>
        ) : ""}
        <span foregroundColor="#555555">{" │ Ctrl+D:setup  ?:help"}</span>
      </text>
    </box>
  );
}
