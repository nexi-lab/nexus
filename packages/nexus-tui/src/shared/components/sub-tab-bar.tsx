/**
 * Shared sub-tab bar component for panel sub-navigation.
 *
 * Render-only — does NOT own keyboard bindings. Panels compose
 * subTabForward/subTabBackward from sub-tab-bar-utils.ts into their
 * own useKeyboard calls.
 *
 * @see Issue #3498
 */

import React from "react";
import type { SubTab } from "./sub-tab-bar-utils.js";

export interface SubTabBarProps {
  /** Visible tabs to render (output of useVisibleTabs). */
  readonly tabs: readonly SubTab[];
  /** Currently active tab ID. */
  readonly activeTab: string;
}

/**
 * Renders a horizontal sub-tab bar with bracket notation for the active tab.
 *
 * Example output: `[Zones]  Bricks   Drift   Reindex`
 */
export function SubTabBar({ tabs, activeTab }: SubTabBarProps): React.ReactNode {
  if (tabs.length === 0) return null;

  return (
    <box height={1} width="100%">
      <text>
        {tabs
          .map((tab) =>
            tab.id === activeTab ? `[${tab.label}]` : ` ${tab.label} `,
          )
          .join(" ")}
      </text>
    </box>
  );
}
