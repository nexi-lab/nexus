/**
 * Subscription list view: shows event subscriptions with status and trigger info.
 */

import React from "react";
import type { Subscription } from "../../stores/infra-store.js";
import { Spinner } from "../../shared/components/spinner.js";

const STATUS_BADGE: Record<string, string> = {
  active: "[ON]",
  paused: "[||]",
  failed: "[!!]",
};

export function SubscriptionList({
  subscriptions,
  selectedIndex,
  loading,
}: {
  readonly subscriptions: readonly Subscription[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}): React.ReactNode {
  if (loading) {
    return <Spinner label="Loading subscriptions..." />;
  }

  if (subscriptions.length === 0) {
    return <text>No subscriptions configured</text>;
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  Status  Event Type           Endpoint                  Triggers"}</text>
      </box>

      {subscriptions.map((sub, i) => {
        const prefix = i === selectedIndex ? "> " : "  ";
        const badge = STATUS_BADGE[sub.status] ?? "[??]";
        const eventType = sub.event_type.padEnd(20).slice(0, 20);
        const endpoint = sub.endpoint.padEnd(25).slice(0, 25);
        const triggers = String(sub.trigger_count).padStart(8);

        return (
          <box key={sub.subscription_id} height={1} width="100%">
            <text>{`${prefix}${badge}  ${eventType} ${endpoint} ${triggers}`}</text>
          </box>
        );
      })}
    </scrollbox>
  );
}
