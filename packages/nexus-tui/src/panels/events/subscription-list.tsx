import { For } from "solid-js";
import type { JSX } from "solid-js";
/**
 * Subscription list view: shows event subscriptions with status and trigger info.
 */

import type { Subscription } from "../../stores/infra-store.js";

const STATUS_BADGE: Record<string, string> = {
  active: "[ON]",
  paused: "[||]",
  failed: "[!!]",
};

export function SubscriptionList(props: {
  readonly subscriptions: readonly Subscription[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading subscriptions..."
          : props.subscriptions.length === 0
            ? "No subscriptions configured"
            : `${props.subscriptions.length} subscriptions`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {/* Header */}
        <box height={1} width="100%">
          <text>{"  Status  Event Type           Endpoint                  Triggers"}</text>
        </box>

        <For each={props.subscriptions}>{(sub, i) => {
          const prefix = () => i() === props.selectedIndex ? "> " : "  ";
          const badge = STATUS_BADGE[sub.status] ?? "[??]";
          const eventType = sub.event_type.padEnd(20).slice(0, 20);
          const endpoint = sub.endpoint.padEnd(25).slice(0, 25);
          const triggers = String(sub.trigger_count).padStart(8);

          return (
            <box height={1} width="100%">
              <text>{`${prefix()}${badge}  ${eventType} ${endpoint} ${triggers}`}</text>
            </box>
          );
        }}</For>
      </scrollbox>
    </box>
  );
}
