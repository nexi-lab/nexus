/**
 * Action hints for the selected transaction.
 *
 * Shows available keyboard shortcuts based on transaction status.
 */

import React from "react";
import type { Transaction } from "../../stores/versions-store.js";

interface TransactionActionsProps {
  readonly transaction: Transaction | null;
}

export function TransactionActions({
  transaction,
}: TransactionActionsProps): React.ReactNode {
  if (!transaction) {
    return <text>{"n:new transaction  f:filter  q:quit"}</text>;
  }

  if (transaction.status === "active") {
    return (
      <text>
        {"Enter:commit  Backspace:rollback  n:new transaction  f:filter  q:quit"}
      </text>
    );
  }

  return (
    <text>
      {"(read-only)  n:new transaction  f:filter  q:quit"}
    </text>
  );
}
