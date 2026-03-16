/**
 * Full-screen keybinding reference overlay.
 *
 * Activated with `?` key. Shows all keybindings for the current panel
 * plus global bindings. Press any key to dismiss.
 *
 * @see Issue #3066, Phase E9
 */

import React from "react";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { statusColor } from "../theme.js";
import type { PanelId } from "../../stores/global-store.js";

interface HelpOverlayProps {
  readonly visible: boolean;
  readonly panel: PanelId;
  readonly onDismiss: () => void;
}

interface KeyBinding {
  readonly key: string;
  readonly action: string;
}

// =============================================================================
// Keybinding definitions per panel
// =============================================================================

const GLOBAL_BINDINGS: readonly KeyBinding[] = [
  { key: "1-9,0", action: "Switch panel" },
  { key: "Ctrl+I", action: "Identity switcher" },
  { key: "z", action: "Toggle zoom" },
  { key: "?", action: "Help overlay" },
  { key: "q", action: "Quit" },
];

const NAV_BINDINGS: readonly KeyBinding[] = [
  { key: "j/↓", action: "Move down" },
  { key: "k/↑", action: "Move up" },
  { key: "gg", action: "Jump to top" },
  { key: "G", action: "Jump to bottom" },
  { key: "Enter", action: "Select/expand" },
  { key: "Tab", action: "Switch pane/tab" },
  { key: "Esc", action: "Cancel/back" },
];

const PANEL_BINDINGS: Record<string, readonly KeyBinding[]> = {
  files: [
    { key: "l/→", action: "Expand folder" },
    { key: "h/←", action: "Collapse folder" },
    { key: "d", action: "Delete file" },
    { key: "Shift+N", action: "New directory" },
    { key: "Shift+R", action: "Rename" },
    { key: "x", action: "Revoke share link" },
  ],
  versions: [
    { key: "n", action: "New transaction" },
    { key: "Enter", action: "Commit transaction" },
    { key: "Backspace", action: "Rollback" },
    { key: "v", action: "View diff" },
    { key: "c", action: "Toggle conflicts" },
    { key: "f", action: "Cycle status filter" },
  ],
  agents: [
    { key: "d", action: "Revoke delegation" },
    { key: "r", action: "Refresh" },
    { key: "Shift+W", action: "Warmup agent" },
    { key: "Shift+E", action: "Evict agent" },
    { key: "Shift+V", action: "Verify agent" },
  ],
  zones: [
    { key: "n", action: "Register new" },
    { key: "d", action: "Unregister" },
    { key: "m", action: "Mount brick" },
    { key: "u", action: "Unmount brick" },
    { key: "x", action: "Reset brick" },
    { key: "r", action: "Remount" },
  ],
  access: [
    { key: "n", action: "New delegation" },
    { key: "Shift+X", action: "Revoke manifest" },
    { key: "x", action: "Revoke credential" },
    { key: "s", action: "Suspend agent" },
    { key: "o", action: "Complete delegation" },
    { key: "v", action: "View chain" },
    { key: "p", action: "Permission check" },
    { key: "f", action: "Cycle filter" },
  ],
  payments: [
    { key: "n", action: "New policy" },
    { key: "d", action: "Delete policy" },
    { key: "t", action: "Transfer funds" },
    { key: "c", action: "Commit reservation" },
    { key: "x", action: "Release reservation" },
    { key: "a", action: "Affordability check" },
    { key: "i", action: "Integrity check" },
    { key: "]", action: "Next page" },
    { key: "[", action: "Previous page" },
  ],
  search: [
    { key: "/", action: "Search" },
    { key: "m", action: "Cycle mode (KW/SEM/HYB)" },
    { key: "n", action: "Create memory" },
    { key: "u", action: "Update memory" },
    { key: "d", action: "Delete" },
    { key: "v", action: "View diff" },
  ],
  workflows: [
    { key: "e", action: "Execute workflow" },
    { key: "d", action: "Delete workflow" },
    { key: "p", action: "Toggle enabled" },
  ],
  infrastructure: [
    { key: "d", action: "Delete subscription" },
    { key: "t", action: "Test subscription" },
    { key: "r", action: "Reconnect SSE" },
    { key: "c", action: "Clear events" },
    { key: "f", action: "Filter by type" },
    { key: "s", action: "Search filter" },
  ],
  console: [
    { key: ":", action: "Command mode" },
    { key: "Enter", action: "Execute request" },
    { key: "/", action: "Filter endpoints" },
  ],
};

// =============================================================================
// Component
// =============================================================================

export function HelpOverlay({
  visible,
  panel,
  onDismiss,
}: HelpOverlayProps): React.ReactNode {
  useKeyboard(
    visible
      ? {
          escape: onDismiss,
          "?": onDismiss,
          // Dismiss on any other key via onUnhandled
        }
      : {},
    visible ? () => onDismiss() : undefined,
  );

  if (!visible) return null;

  const panelBindings = PANEL_BINDINGS[panel] ?? [];

  return (
    <box
      height="100%"
      width="100%"
      justifyContent="center"
      alignItems="center"
    >
      <box
        flexDirection="column"
        borderStyle="double"
        width={60}
        padding={1}
      >
        <text bold>Keybinding Reference</text>
        <text>{""}</text>

        <text foregroundColor={statusColor.info} bold>{"─── Global ───"}</text>
        {GLOBAL_BINDINGS.map((b) => (
          <text key={b.key}>
            <text foregroundColor={statusColor.info}>{`  ${b.key.padEnd(12)}`}</text>
            <text>{b.action}</text>
          </text>
        ))}

        <text>{""}</text>
        <text foregroundColor={statusColor.info} bold>{"─── Navigation ───"}</text>
        {NAV_BINDINGS.map((b) => (
          <text key={b.key}>
            <text foregroundColor={statusColor.info}>{`  ${b.key.padEnd(12)}`}</text>
            <text>{b.action}</text>
          </text>
        ))}

        {panelBindings.length > 0 && (
          <>
            <text>{""}</text>
            <text foregroundColor={statusColor.info} bold>{`─── ${panel} ───`}</text>
            {panelBindings.map((b) => (
              <text key={b.key}>
                <text foregroundColor={statusColor.info}>{`  ${b.key.padEnd(12)}`}</text>
                <text>{b.action}</text>
              </text>
            ))}
          </>
        )}

        <text>{""}</text>
        <text dimColor>Press any key to dismiss</text>
      </box>
    </box>
  );
}
