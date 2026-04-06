/**
 * Balance card: displays available, reserved, and total credit amounts.
 */

import type { BalanceInfo } from "../../stores/payments-store.js";

interface BalanceCardProps {
  readonly balance: BalanceInfo | null;
  readonly loading: boolean;
}

export function BalanceCard(props: BalanceCardProps) {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading balance..."
          : !props.balance
            ? "No balance data available"
            : "--- Balances ---"}
      </text>

      {(() => {
        if (props.loading || !props.balance) return null;
        return (
          <>
            <box height={1} width="100%">
              <text>{`Available:  ${props.balance.available}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{`Reserved:   ${props.balance.reserved}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{`Total:      ${props.balance.total}`}</text>
            </box>
          </>
        );
      })()}
    </box>
  );
}
