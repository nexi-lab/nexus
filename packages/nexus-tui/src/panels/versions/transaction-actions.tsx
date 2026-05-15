/**
 * Action hints for the selected transaction.
 *
 * Shows available keyboard shortcuts based on transaction status.
 */

import { Show } from "solid-js";
import type { Transaction } from "../../stores/versions-store.js";

interface TransactionActionsProps {
  readonly transaction: Transaction | null;
}

export function TransactionActions(props: TransactionActionsProps) {
  return (
    <Show
      when={props.transaction}
      fallback={<text>{"n:new transaction  f:filter  q:quit"}</text>}
    >
      <Show
        when={props.transaction!.status === "active"}
        fallback={
          <text>
            {"(read-only)  n:new transaction  f:filter  q:quit"}
          </text>
        }
      >
        <text>
          {"Enter:commit  Backspace:rollback  n:new transaction  f:filter  q:quit"}
        </text>
      </Show>
    </Show>
  );
}
