/**
 * Subscriptions tab: event subscription list with delete/test actions.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import { createEffect } from "solid-js";
import type { JSX } from "solid-js";
import { useInfraStore } from "../../stores/infra-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { SubscriptionList } from "./subscription-list.js";

interface SubscriptionsTabProps {
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function SubscriptionsTab(props: SubscriptionsTabProps): JSX.Element {
  const client = useApi();
  const confirm = useConfirmStore((s) => s.confirm);

  const subscriptions = () => useInfraStore((s) => s.subscriptions);
  const subscriptionsLoading = () => useInfraStore((s) => s.subscriptionsLoading);
  const selectedSubscriptionIndex = () => useInfraStore((s) => s.selectedSubscriptionIndex);
  const setSelectedSubscriptionIndex = useInfraStore((s) => s.setSelectedSubscriptionIndex);
  const fetchSubscriptions = useInfraStore((s) => s.fetchSubscriptions);
  const deleteSubscription = useInfraStore((s) => s.deleteSubscription);
  const testSubscription = useInfraStore((s) => s.testSubscription);

  createEffect(() => {
    if (client) fetchSubscriptions(client);
  });

  useKeyboard(
    (): Record<string, () => void> => {
      if (props.overlayActive) return {};
      const listNav = listNavigationBindings({
        getIndex: () => selectedSubscriptionIndex(),
        setIndex: (i) => setSelectedSubscriptionIndex(i),
        getLength: () => subscriptions().length,
      });
      return {
        ...listNav,
        ...props.tabBindings,
        d: async () => {
          if (client) {
            const sub = subscriptions()[selectedSubscriptionIndex()];
            if (sub) {
              const ok = await confirm("Delete subscription?", "Delete this event subscription.");
              if (!ok) return;
              deleteSubscription(sub.subscription_id, client);
            }
          }
        },
        t: () => {
          if (client) {
            const sub = subscriptions()[selectedSubscriptionIndex()];
            if (sub) testSubscription(sub.subscription_id, client);
          }
        },
        r: () => { if (client) fetchSubscriptions(client); },
      };
    },
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box flexGrow={1} width="100%" borderStyle="single">
        <SubscriptionList
          subscriptions={subscriptions()}
          selectedIndex={selectedSubscriptionIndex()}
          loading={subscriptionsLoading()}
        />
      </box>
      <box height={1} width="100%">
        <text>{"j/k:navigate  d:delete  t:test  r:refresh  Tab:switch tab"}</text>
      </box>
    </box>
  );
}
