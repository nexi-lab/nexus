/**
 * Budget card: displays spending limits vs spent vs remaining for each period.
 */

import React from "react";
import type { BudgetSummary } from "../../stores/payments-store.js";

interface BudgetCardProps {
  readonly budget: BudgetSummary | null;
  readonly loading: boolean;
}

export function BudgetCard({ budget, loading }: BudgetCardProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading budget...</text>
      </box>
    );
  }

  if (!budget) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No budget data available. Press b to fetch.</text>
      </box>
    );
  }

  if (!budget.has_policy) {
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
        <text>{`${"Daily".padEnd(9)}  ${budget.limits.daily.padEnd(13)}  ${budget.spent.daily.padEnd(13)}  ${budget.remaining.daily}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`${"Weekly".padEnd(9)}  ${budget.limits.weekly.padEnd(13)}  ${budget.spent.weekly.padEnd(13)}  ${budget.remaining.weekly}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`${"Monthly".padEnd(9)}  ${budget.limits.monthly.padEnd(13)}  ${budget.spent.monthly.padEnd(13)}  ${budget.remaining.monthly}`}</text>
      </box>

      {budget.policy_id && (
        <box height={1} width="100%" marginTop={1}>
          <text>{`Policy: ${budget.policy_id}`}</text>
        </box>
      )}
    </scrollbox>
  );
}
