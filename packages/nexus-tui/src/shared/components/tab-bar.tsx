/**
 * Horizontal tab bar for switching between panels.
 *
 * Each tab is rendered as a <box> element so that onMouseDown can fire
 * per-tab click events.  The visual output is identical to the previous
 * <text><span> layout.
 *
 * Enhanced with semantic colors (Phase A1).
 */

import { For, Show } from "solid-js";
import { palette } from "../theme.js";
import { textStyle } from "../text-style.js";

export interface Tab {
  readonly id: string;
  readonly label: string;
  readonly shortcut: string;
}

interface TabBarProps {
  readonly tabs: readonly Tab[];
  readonly activeTab: string;
  readonly onSelect: (id: string) => void;
}

export function TabBar(props: TabBarProps) {
  return (
    <box height={1} width="100%" flexDirection="row">
      <For each={props.tabs}>{(tab, index) => {
        const isActive = () => tab.id === props.activeTab;
        const suffix = () => index() < props.tabs.length - 1 ? " │ " : "";
        return (
          <box height={1} onMouseDown={() => props.onSelect(tab.id)}>
            <Show
              when={isActive()}
              fallback={
                <text>
                  <span style={textStyle({ fg: palette.muted })}>{`  ${tab.shortcut}:${tab.label}`}</span>
                  <span style={textStyle({ fg: palette.faint })}>{suffix()}</span>
                </text>
              }
            >
              <text>
                <span style={textStyle({ fg: palette.accent, bold: true })}>{"▸ "}</span>
                <span style={textStyle({ fg: palette.muted })}>{`${tab.shortcut}:`}</span>
                <span style={textStyle({ fg: palette.accent, bold: true })}>{tab.label}</span>
                <span style={textStyle({ fg: palette.faint })}>{suffix()}</span>
              </text>
            </Show>
          </box>
        );
      }}</For>
    </box>
  );
}
