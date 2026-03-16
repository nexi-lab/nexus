/**
 * Balance card: displays available, reserved, and total credit amounts.
 */

import React from "react";
import type { BalanceInfo } from "../../stores/payments-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";

interface BalanceCardProps {
  readonly balance: BalanceInfo | null;
  readonly loading: boolean;
}

export function BalanceCard({ balance, loading }: BalanceCardProps): React.ReactNode {
  if (loading) {
    return <LoadingIndicator message="Loading balance..." />;
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
      <box height={1} width="100%" marginTop={1}>
        <text>--- Balances ---</text>
      </box>
      <box height={1} width="100%">
        <text>{`Available:  ${balance.available}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Reserved:   ${balance.reserved}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Total:      ${balance.total}`}</text>
      </box>
    </scrollbox>
  );
}
