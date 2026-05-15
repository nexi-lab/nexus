/**
 * Shared sub-tab bar component for panel sub-navigation.
 *
 * Render-only — does NOT own keyboard bindings. Panels compose
 * subTabForward/subTabBackward from sub-tab-bar-utils.ts into their
 * own useKeyboard calls.
 *
 * @see Issue #3498
 */

import { For, Show } from "solid-js";
import type { SubTab } from "./sub-tab-bar-utils.js";

export interface SubTabBarProps {
  /** Visible tabs to render (output of useVisibleTabs). */
  readonly tabs: readonly SubTab[];
  /** Currently active tab ID. */
  readonly activeTab: string;
  /** Called with the tab id when a tab is clicked. */
  readonly onSelect?: (id: string) => void;
}

/**
 * Renders a horizontal sub-tab bar with bracket notation for the active tab.
 *
 * Example output: `[Zones]  Bricks   Drift   Reindex`
 */
export function SubTabBar(props: SubTabBarProps) {
  return (
    <Show when={props.tabs.length > 0}>
      <box height={1} width="100%" flexDirection="row">
        <For each={props.tabs}>{(tab, index) => {
          const isActive = () => tab.id === props.activeTab;
          const isLast = () => index() === props.tabs.length - 1;
          const label = () => isActive() ? `[${tab.label}]` : ` ${tab.label} `;
          const content = () => isLast() ? label() : `${label()} `;
          return (
            <box height={1} onMouseDown={() => props.onSelect?.(tab.id)}>
              <text>{content()}</text>
            </box>
          );
        }}</For>
      </box>
    </Show>
  );
}
