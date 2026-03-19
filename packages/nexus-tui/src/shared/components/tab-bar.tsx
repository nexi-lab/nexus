/**
 * Horizontal tab bar for switching between panels.
 *
 * Enhanced with semantic colors (Phase A1).
 */

import React from "react";
import { palette } from "../theme.js";

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

export function TabBar({ tabs, activeTab }: TabBarProps): React.ReactNode {
  return (
    <box height={1} width="100%">
      <text>
        {tabs.map((tab, index) => {
          const isActive = tab.id === activeTab;
          const suffix = index < tabs.length - 1 ? " │ " : "";
          if (isActive) {
            return (
              <span key={tab.id}>
                <span foregroundColor={palette.accent} bold>{"▸ "}</span>
                <span foregroundColor={palette.muted}>{`${tab.shortcut}:`}</span>
                <span foregroundColor={palette.accent} bold>{tab.label}</span>
                <span foregroundColor={palette.faint}>{suffix}</span>
              </span>
            );
          }
          return (
            <span key={tab.id}>
              <span foregroundColor={palette.muted}>{`  ${tab.shortcut}:${tab.label}`}</span>
              <span foregroundColor={palette.faint}>{suffix}</span>
            </span>
          );
        })}
      </text>
    </box>
  );
}
