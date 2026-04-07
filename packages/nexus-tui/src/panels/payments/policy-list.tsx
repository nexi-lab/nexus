/**
 * Policy list: displays spending policy records with limits and enabled status.
 */

import { For } from "solid-js";
import type { PolicyRecord } from "../../stores/payments-store.js";

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
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading policies..."
          : props.policies.length === 0
            ? "No policies yet. Press Shift+N to create a policy."
            : `${props.policies.length} policies`}
      </text>
      <scrollbox flexGrow={1} width="100%">
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
    </box>
  );
}
