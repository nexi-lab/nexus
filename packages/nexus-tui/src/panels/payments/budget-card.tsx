/**
 * Budget card: displays spending limits vs spent vs remaining for each period.
 */

import { Show } from "solid-js";
import type { BudgetSummary } from "../../stores/payments-store.js";

interface BudgetCardProps {
  readonly budget: BudgetSummary | null;
  readonly loading: boolean;
}

export function BudgetCard(props: BudgetCardProps) {
  if (props.loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading budget...</text>
      </box>
    );
  }

  if (!props.budget) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No budget data available. Press b to fetch.</text>
      </box>
    );
  }

  if (!props.budget.has_policy) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No spending policy configured</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      <box height={1} width="100%" marginTop={1}>
        <text>--- Budget Summary ---</text>
      </box>

      {/* Header */}
      <box height={1} width="100%" marginTop={1}>
        <text>{"PERIOD     LIMIT          SPENT          REMAINING"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"---------  -------------  -------------  -------------"}</text>
      </box>

      {/* Rows */}
      <box height={1} width="100%">
        <text>{`${"Daily".padEnd(9)}  ${props.budget.limits.daily.padEnd(13)}  ${props.budget.spent.daily.padEnd(13)}  ${props.budget.remaining.daily}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`${"Weekly".padEnd(9)}  ${props.budget.limits.weekly.padEnd(13)}  ${props.budget.spent.weekly.padEnd(13)}  ${props.budget.remaining.weekly}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`${"Monthly".padEnd(9)}  ${props.budget.limits.monthly.padEnd(13)}  ${props.budget.spent.monthly.padEnd(13)}  ${props.budget.remaining.monthly}`}</text>
      </box>

      <Show when={props.budget.policy_id}>
        <box height={1} width="100%" marginTop={1}>
          <text>{`Policy: ${props.budget.policy_id}`}</text>
        </box>
      </Show>
    </scrollbox>
  );
}
