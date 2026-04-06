/**
 * Policy list: displays spending policy records with limits and enabled status.
 */

import { For } from "solid-js";
import type { PolicyRecord } from "../../stores/payments-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { EmptyState } from "../../shared/components/empty-state.js";

interface PolicyListProps {
  readonly policies: readonly PolicyRecord[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function shortId(id: string): string {
  if (id.length <= 12) return id;
  return `${id.slice(0, 8)}..`;
}

export function PolicyList(props: PolicyListProps) {
  if (props.loading) {
    return <LoadingIndicator message="Loading policies..." />;
  }

  if (props.policies.length === 0) {
    return <EmptyState message="No policies yet." hint="Press Shift+N to create a policy." />;
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  ID          DAILY        WEEKLY       MONTHLY      PER-TX       ENABLED"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ----------  -----------  -----------  -----------  -----------  -------"}</text>
      </box>

      {/* Rows */}
      <For each={props.policies}>{(p, i) => {
        const isSelected = () => i() === props.selectedIndex;
        const enabled = p.enabled ? "yes" : "no";
        const daily = (p.daily_limit ?? "-").padEnd(11);
        const weekly = (p.weekly_limit ?? "-").padEnd(11);
        const monthly = (p.monthly_limit ?? "-").padEnd(11);
        const perTx = (p.per_tx_limit ?? "-").padEnd(11);

        return (
          <box height={1} width="100%">
            <text>
              {`${isSelected() ? "> " : "  "}${shortId(p.policy_id).padEnd(10)}  ${daily}  ${weekly}  ${monthly}  ${perTx}  ${enabled}`}
            </text>
          </box>
        );
      }}</For>
    </scrollbox>
  );
}
