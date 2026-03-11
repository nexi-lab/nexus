/**
 * Balance card: displays available, pending, and reserved credit amounts.
 */

import React from "react";
import type { BalanceInfo } from "../../stores/payments-store.js";

interface BalanceCardProps {
  readonly balance: BalanceInfo | null;
  readonly loading: boolean;
}

function formatTimestamp(ts: string | null): string {
  if (!ts) return "n/a";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function BalanceCard({ balance, loading }: BalanceCardProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading balance...</text>
      </box>
    );
  }

  if (!balance) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No balance data available</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      <box height={1} width="100%">
        <text>{`Account: ${balance.account_id}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Currency: ${balance.currency}`}</text>
      </box>

      <box height={1} width="100%" marginTop={1}>
        <text>--- Balances ---</text>
      </box>
      <box height={1} width="100%">
        <text>{`Available:  ${balance.available}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Pending:    ${balance.pending}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Reserved:   ${balance.reserved}`}</text>
      </box>

      <box height={1} width="100%" marginTop={1}>
        <text>{`Last updated: ${formatTimestamp(balance.updated_at)}`}</text>
      </box>
    </scrollbox>
  );
}
