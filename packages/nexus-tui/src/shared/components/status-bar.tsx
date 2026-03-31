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
import { useAccessStore } from "../../stores/access-store.js";
import { useAgentsStore } from "../../stores/agents-store.js";
import { usePaymentsStore } from "../../stores/payments-store.js";
import { useSearchStore } from "../../stores/search-store.js";
import { useWorkflowsStore } from "../../stores/workflows-store.js";
import { useZonesStore } from "../../stores/zones-store.js";
import { useInfraStore } from "../../stores/infra-store.js";
import { connectionColor, palette, statusColor } from "../theme.js";
import { deriveStatusBreadcrumb } from "../status-breadcrumb.js";

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
  const profile = useGlobalStore((s) => s.profile);
  const mode = useGlobalStore((s) => s.mode);
  const accessTab = useAccessStore((s) => s.activeTab);
  const agentTab = useAgentsStore((s) => s.activeTab);
  const paymentsTab = usePaymentsStore((s) => s.activeTab);
  const searchTab = useSearchStore((s) => s.activeTab);
  const workflowTab = useWorkflowsStore((s) => s.activeTab);
  const zoneTab = useZonesStore((s) => s.activeTab);
  const eventsTab = useInfraStore((s) => s.activePanelTab);

  // Check if events panel has active filters
  const eventFilters = useEventsStore((s) => s.filters);
  const hasActiveFilter = eventFilters.eventType !== null || eventFilters.search !== null;

  const icon = STATUS_ICONS[status] ?? "?";
  const baseUrl = config.baseUrl ?? "localhost:2026";
  const breadcrumb = deriveStatusBreadcrumb({
    connectionStatus: status,
    activePanel,
    accessTab,
    agentTab,
    paymentsTab,
    searchTab,
    workflowTab,
    zoneTab,
    eventsTab,
  });

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
        {breadcrumb ? (
          <>
            <span dimColor>{" │ "}</span>
            <span foregroundColor={statusColor.info}>{breadcrumb}</span>
          </>
        ) : ""}
        {identityParts.length > 0 ? (
          <>
            <span dimColor>{" │ "}</span>
            <span foregroundColor={statusColor.identity}>{identityParts.join(", ")}</span>
          </>
        ) : ""}
        {serverVersion ? (
          <>
            <span dimColor>{" │ "}</span>
            <span dimColor>{`v${serverVersion}${profile ? `/${profile}` : ""}${mode ? `/${mode}` : ""}`}</span>
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
        {hasActiveFilter ? (
          <span foregroundColor="yellow">{" [filtered]"}</span>
        ) : ""}
      </text>
      <box flexGrow={1} />
      <text dimColor>{"? Help"}</text>
    </box>
  );
}
