/**
 * Bottom status bar showing connection state, active identity, and path.
 *
 * Enhanced with semantic colors from theme.ts (Phase A1).
 *
 * Note: OpenTUI does not support nested <text> elements. Use <span> for
 * inline styled segments inside a <text>.
 */

import { createSignal, onCleanup } from "solid-js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useEventsStore } from "../../stores/events-store.js";
import { connectionColor, palette, statusColor } from "../theme.js";
import { textStyle } from "../text-style.js";

const MIN_COLS = 80;
const MIN_ROWS = 24;

const STATUS_ICONS: Record<string, string> = {
  connected: "●",
  connecting: "◐",
  disconnected: "○",
  error: "✗",
};

export function StatusBar() {
  const status = useGlobalStore((s) => s.connectionStatus);
  const config = useGlobalStore((s) => s.config);
  const serverVersion = useGlobalStore((s) => s.serverVersion);
  const zoneId = useGlobalStore((s) => s.zoneId);
  const activePanel = useGlobalStore((s) => s.activePanel);
  const userInfo = useGlobalStore((s) => s.userInfo);
  const enabledBricks = useGlobalStore((s) => s.enabledBricks);
  const profile = useGlobalStore((s) => s.profile);
  const mode = useGlobalStore((s) => s.mode);

  // Check if events panel has active filters
  const eventFilters = useEventsStore((s) => s.filters);
  const hasActiveFilter = eventFilters.eventType !== null || eventFilters.search !== null;

  // Terminal size guard (#3245)
  const [terminalTooSmall, setTerminalTooSmall] = createSignal(false);
  const check = () => {
    const cols = process.stdout.columns ?? 80;
    const rows = process.stdout.rows ?? 24;
    setTerminalTooSmall(cols < MIN_COLS || rows < MIN_ROWS);
  };
  check();
  process.stdout.on("resize", check);
  onCleanup(() => { process.stdout.off("resize", check); });

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
        {terminalTooSmall() ? (
          <span style={textStyle({ fg: statusColor.warning })}>{`⚠ Terminal too small (need ${MIN_COLS}×${MIN_ROWS}) `}</span>
        ) : ""}
        <span style={textStyle({ fg: connectionColor[status] })}>{icon}</span>
        <span style={textStyle({ dim: true })}>{` ${status} │ `}</span>
        <span>{baseUrl}</span>
        {identityParts.length > 0 ? (
          <>
            <span style={textStyle({ dim: true })}>{" │ "}</span>
            <span style={textStyle({ fg: statusColor.identity })}>{identityParts.join(", ")}</span>
          </>
        ) : ""}
        {serverVersion ? (
          <>
            <span style={textStyle({ dim: true })}>{" │ "}</span>
            <span style={textStyle({ dim: true })}>{`v${serverVersion}${profile ? `/${profile}` : ""}${mode ? `/${mode}` : ""}`}</span>
          </>
        ) : ""}
        {zone ? (
          <>
            <span style={textStyle({ dim: true })}>{" │ "}</span>
            <span style={textStyle({ fg: statusColor.reference })}>{`zone:${zone}`}</span>
          </>
        ) : ""}
        {enabledBricks.length > 0 ? (
          <>
            <span style={textStyle({ dim: true })}>{" │ "}</span>
            <span style={textStyle({ fg: statusColor.info })}>{`${enabledBricks.length} bricks`}</span>
          </>
        ) : ""}
        <span style={textStyle({ dim: true })}>{" │ "}</span>
        <span style={textStyle({ fg: statusColor.info })}>{`[${activePanel}]`}</span>
        {hasActiveFilter ? (
          <span style={textStyle({ fg: "yellow" })}>{" [filtered]"}</span>
        ) : ""}
        <span style={textStyle({ fg: palette.faint })}>{" │ Ctrl+D:setup  ?:help"}</span>
      </text>
    </box>
  );
}
