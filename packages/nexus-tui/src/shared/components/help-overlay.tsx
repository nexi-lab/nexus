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
import { textStyle } from "../text-style.js";
import type { PanelId } from "../../stores/global-store.js";
import {
  type KeyBinding,
  GLOBAL_BINDINGS,
  NAV_BINDINGS,
  PANEL_BINDINGS,
} from "../action-registry.js";

interface HelpOverlayProps {
  readonly visible: boolean;
  readonly panel: PanelId;
  readonly onDismiss: () => void;
}

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
        <text style={textStyle({ bold: true })}>Keybinding Reference</text>
        <text>{""}</text>

        <text style={textStyle({ fg: statusColor.info, bold: true })}>{"─── Global ───"}</text>
        {GLOBAL_BINDINGS.map((b) => (
          <text key={b.key}>
            <span style={textStyle({ fg: statusColor.info })}>{`  ${b.key.padEnd(12)}`}</span>
            <span>{b.action}</span>
          </text>
        ))}

        <text>{""}</text>
        <text style={textStyle({ fg: statusColor.info, bold: true })}>{"─── Navigation ───"}</text>
        {NAV_BINDINGS.map((b) => (
          <text key={b.key}>
            <span style={textStyle({ fg: statusColor.info })}>{`  ${b.key.padEnd(12)}`}</span>
            <span>{b.action}</span>
          </text>
        ))}

        {panelBindings.length > 0 && (
          <>
            <text>{""}</text>
            <text style={textStyle({ fg: statusColor.info, bold: true })}>{`─── ${panel} ───`}</text>
            {panelBindings.map((b) => (
              <text key={b.key}>
                <span style={textStyle({ fg: statusColor.info })}>{`  ${b.key.padEnd(12)}`}</span>
                <span>{b.action}</span>
              </text>
            ))}
          </>
        )}

        <text>{""}</text>
        <text style={textStyle({ dim: true })}>Press any key to dismiss</text>
      </box>
    </box>
  );
}
