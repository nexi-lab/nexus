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
  // Read store via getState() inside accessors — component body runs once in SolidJS.
  const gs = () => useGlobalStore.getState();
  const status = () => gs().connectionStatus;
  const config = () => gs().config;
  const serverVersion = () => gs().serverVersion;
  const zoneId = () => gs().zoneId;
  const activePanel = () => gs().activePanel;
  const userInfo = () => gs().userInfo;
  const enabledBricks = () => gs().enabledBricks;
  const profile = () => gs().profile;
  const mode = () => gs().mode;

  const hasActiveFilter = () => {
    const f = useEventsStore.getState().filters;
    return f.eventType !== null || f.search !== null;
  };

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

  const icon = () => STATUS_ICONS[status()] ?? "?";
  const baseUrl = () => config().baseUrl ?? "localhost:2026";

  const identityParts = (): string[] => {
    const ui = userInfo();
    const cfg = config();
    const parts: string[] = [];
    if (ui?.display_name ?? ui?.username) {
      parts.push(ui!.display_name ?? ui!.username!);
    } else if (cfg.agentId) {
      parts.push(`agent:${cfg.agentId}`);
    }
    if (cfg.subject && cfg.subject !== cfg.agentId) {
      parts.push(`sub:${cfg.subject}`);
    }
    return parts;
  };

  const zone = () => config().zoneId ?? zoneId();

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
        <span style={textStyle({ fg: connectionColor[status()] })}>{icon()}</span>
        <span style={textStyle({ dim: true })}>{` ${status()} │ `}</span>
        <span>{baseUrl()}</span>
        {identityParts().length > 0 ? (
          <>
            <span style={textStyle({ dim: true })}>{" │ "}</span>
            <span style={textStyle({ fg: statusColor.identity })}>{identityParts().join(", ")}</span>
          </>
        ) : ""}
        {serverVersion() ? (
          <>
            <span style={textStyle({ dim: true })}>{" │ "}</span>
            <span style={textStyle({ dim: true })}>{`v${serverVersion()}${profile() ? `/${profile()}` : ""}${mode() ? `/${mode()}` : ""}`}</span>
          </>
        ) : ""}
        {zone() ? (
          <>
            <span style={textStyle({ dim: true })}>{" │ "}</span>
            <span style={textStyle({ fg: statusColor.reference })}>{`zone:${zone()}`}</span>
          </>
        ) : ""}
        {enabledBricks().length > 0 ? (
          <>
            <span style={textStyle({ dim: true })}>{" │ "}</span>
            <span style={textStyle({ fg: statusColor.info })}>{`${enabledBricks().length} bricks`}</span>
          </>
        ) : ""}
        <span style={textStyle({ dim: true })}>{" │ "}</span>
        <span style={textStyle({ fg: statusColor.info })}>{`[${activePanel()}]`}</span>
        {hasActiveFilter() ? (
          <span style={textStyle({ fg: "yellow" })}>{" [filtered]"}</span>
        ) : ""}
        <span style={textStyle({ fg: palette.faint })}>{" │ Ctrl+D:setup  ?:help"}</span>
      </text>
    </box>
  );
}
