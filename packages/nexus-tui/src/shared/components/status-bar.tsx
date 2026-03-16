/**
 * Bottom status bar showing connection state, active identity, and path.
 */

import React from "react";
import { useGlobalStore } from "../../stores/global-store.js";

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

  const icon = STATUS_ICONS[status] ?? "?";
  const baseUrl = config.baseUrl ?? "localhost:2026";

  const parts: string[] = [
    `${icon} ${status}`,
    baseUrl,
  ];

  // Identity: prefer userInfo from /auth/me, fall back to config headers
  if (userInfo?.display_name ?? userInfo?.username) {
    parts.push(userInfo.display_name ?? userInfo.username!);
  } else if (config.agentId) {
    parts.push(`agent:${config.agentId}`);
  }
  if (config.subject && config.subject !== config.agentId) {
    parts.push(`sub:${config.subject}`);
  }

  if (serverVersion) parts.push(`v${serverVersion}`);
  if (config.zoneId) {
    parts.push(`zone:${config.zoneId}`);
  } else if (zoneId) {
    parts.push(`zone:${zoneId}`);
  }
  parts.push(`[${activePanel}]`);

  return (
    <box
      height={1}
      width="100%"
      flexDirection="row"
      justifyContent="space-between"
    >
      <text>{parts.join(" │ ")}</text>
    </box>
  );
}
