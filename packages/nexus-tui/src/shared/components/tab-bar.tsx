/**
 * Horizontal tab bar for switching between panels.
 *
 * Enhanced with semantic colors (Phase A1) and responsive labels (#3243).
 * Shows full labels (e.g. "Versions") when the terminal is wide enough,
 * falling back to abbreviations (e.g. "Ver") on narrow terminals.
 */

import React from "react";
import { palette } from "../theme.js";
import { useTerminalColumns } from "../hooks/use-terminal-columns.js";
import { shouldUseFullLabels } from "./tab-bar-utils.js";

export type { Tab } from "./tab-bar-utils.js";

interface TabBarProps {
  readonly tabs: readonly import("./tab-bar-utils.js").Tab[];
  readonly activeTab: string;
  readonly onSelect: (id: string) => void;
}

export function TabBar({ tabs, activeTab }: TabBarProps): React.ReactNode {
  const columns = useTerminalColumns();
  const fullLabels = shouldUseFullLabels(tabs, columns);

  return (
    <box height={1} width="100%">
      <text>
        {tabs.map((tab, index) => {
          const isActive = tab.id === activeTab;
          const label = fullLabels ? (tab.fullLabel ?? tab.label) : tab.label;
          const suffix = index < tabs.length - 1 ? " │ " : "";
          if (isActive) {
            return (
              <span key={tab.id}>
                <span foregroundColor={palette.accent} bold>{"▸ "}</span>
                <span foregroundColor={palette.muted}>{`${tab.shortcut}:`}</span>
                <span foregroundColor={palette.accent} bold>{label}</span>
                <span foregroundColor={palette.faint}>{suffix}</span>
              </span>
            );
          }
          return (
            <span key={tab.id}>
              <span foregroundColor={palette.muted}>{`  ${tab.shortcut}:${label}`}</span>
              <span foregroundColor={palette.faint}>{suffix}</span>
            </span>
          );
        })}
      </text>
    </box>
  );
}
