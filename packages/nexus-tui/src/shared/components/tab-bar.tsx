/**
 * Horizontal tab bar for switching between panels.
 *
 * Enhanced with semantic colors (Phase A1).
 */

import React from "react";
import { statusColor } from "../theme.js";

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
    <box height={1} width="100%" flexDirection="row">
      {tabs.map((tab, index) => {
        const isActive = tab.id === activeTab;
        const suffix = index < tabs.length - 1 ? " │" : "";
        return (
          <text key={tab.id}>
            {isActive ? (
              <text>
                <text foregroundColor={statusColor.info}>{"▸ "}</text>
                <text dimColor>{`${tab.shortcut}:`}</text>
                <text foregroundColor={statusColor.info} bold>{tab.label}</text>
                <text dimColor>{suffix}</text>
              </text>
            ) : (
              <text>
                <text>{"  "}</text>
                <text dimColor>{`${tab.shortcut}:${tab.label}${suffix}`}</text>
              </text>
            )}
          </text>
        );
      })}
    </box>
  );
}
