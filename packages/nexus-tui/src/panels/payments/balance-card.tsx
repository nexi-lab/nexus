/**
 * Balance card: displays available, reserved, and total credit amounts.
 */

import { Show } from "solid-js";
import type { BalanceInfo } from "../../stores/payments-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";

interface BalanceCardProps {
  readonly balance: BalanceInfo | null;
  readonly loading: boolean;
}

export function BalanceCard(props: BalanceCardProps) {
  if (props.loading) {
    return <LoadingIndicator message="Loading balance..." />;
  }

  if (!props.balance) {
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
        <text>{`Available:  ${props.balance.available}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Reserved:   ${props.balance.reserved}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`Total:      ${props.balance.total}`}</text>
      </box>
    </scrollbox>
  );
}
