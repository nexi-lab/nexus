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
              <span>
                <span foregroundColor="#00d4ff" bold>{"▸ "}</span>
                <span foregroundColor="#888888">{`${tab.shortcut}:`}</span>
                <span foregroundColor="#00d4ff" bold>{tab.label}</span>
                <span foregroundColor="#444444">{suffix}</span>
              </span>
            ) : (
              <span>
                <span>{"  "}</span>
                <span foregroundColor="#555555">{`${tab.shortcut}:`}</span>
                <span foregroundColor="#777777">{tab.label}</span>
                <span foregroundColor="#444444">{suffix}</span>
              </span>
            )}
          </text>
        );
      })}
    </box>
  );
}
