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
  // Render all tabs as spans inside a single <text> to avoid flex layout
  // width issues that can cause some tab labels to disappear.
  return (
    <box height={1} width="100%">
      <text>
        {tabs.map((tab, index) => {
          const isActive = tab.id === activeTab;
          const suffix = index < tabs.length - 1 ? " │ " : "";
          if (isActive) {
            return (
              <span key={tab.id}>
                <span foregroundColor="#00d4ff" bold>{"▸ "}</span>
                <span foregroundColor="#888888">{`${tab.shortcut}:`}</span>
                <span foregroundColor="#00d4ff" bold>{tab.label}</span>
                <span foregroundColor="#555555">{suffix}</span>
              </span>
            );
          }
          return (
            <span key={tab.id}>
              <span foregroundColor="#999999">{`  ${tab.shortcut}:${tab.label}`}</span>
              <span foregroundColor="#555555">{suffix}</span>
            </span>
          );
        })}
      </text>
    </box>
  );
}
