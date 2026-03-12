/**
 * Horizontal tab bar for switching between panels.
 */

import React from "react";

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
        const prefix = isActive ? "▸ " : "  ";
        const suffix = index < tabs.length - 1 ? " │" : "";
        return (
          <text key={tab.id}>
            {`${prefix}${tab.shortcut}:${tab.label}${suffix}`}
          </text>
        );
      })}
    </box>
  );
}
