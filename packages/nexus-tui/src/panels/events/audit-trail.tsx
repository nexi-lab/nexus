import { Show, For } from "solid-js";
import type { JSX } from "solid-js";
/**
 * Audit trail explorer view.
 *
 * Shows full audit transactions from GET /api/v2/audit/transactions
 * with cursor-based pagination.
 */


import type { AuditTransaction } from "../../stores/infra-store.js";
import { Spinner } from "../../shared/components/spinner.js";
import { EmptyState } from "../../shared/components/empty-state.js";
import { textStyle } from "../../shared/text-style.js";
import { formatTimestamp } from "../../shared/utils/format-time.js";

export interface AuditTrailProps {
  readonly transactions: readonly AuditTransaction[];
  readonly loading: boolean;
  readonly hasMore: boolean;
  readonly selectedIndex: number;
}

export function AuditTrail(props: AuditTrailProps): JSX.Element {
  const displayTransactions = () => props.transactions.slice(0, 200);
  const isTruncated = () => props.transactions.length > 200;

  return (
    <Show
      when={!(props.loading && props.transactions.length === 0)}
      fallback={<Spinner label="Loading audit transactions..." />}
    >
      <Show
        when={props.transactions.length > 0}
        fallback={
          <EmptyState
            message="No audit transactions found."
            hint="Transactions will appear as actions are audited."
          />
        }
      >
        <box flexDirection="column" height="100%" width="100%">
          {/* Header */}
          <box height={1} width="100%">
            <text>
              {isTruncated()
                ? `  Showing first 200 of ${props.transactions.length} — Action           Actor              Resource                       Status    Time`
                : "  Action           Actor              Resource                       Status    Time"}
            </text>
          </box>

          {/* Rows */}
          <scrollbox flexGrow={1} width="100%">
            <For each={displayTransactions()}>{(tx, i) => {
              const prefix = () => i() === props.selectedIndex ? "> " : "  ";
              const action = tx.action.padEnd(16).slice(0, 16);
              const actor = tx.actor_id.padEnd(18).slice(0, 18);
              const resource = tx.resource.padEnd(30).slice(0, 30);
              const status = tx.status.padEnd(9).slice(0, 9);
              const time = formatTimestamp(tx.timestamp);
              return (
                <box height={1} width="100%">
                  <text>{`${prefix()}${action} ${actor} ${resource} ${status} ${time}`}</text>
                </box>
              );
            }}</For>
            <Show when={props.hasMore}>
              <text style={textStyle({ dim: true })}>{"  ... more transactions (press m to load more)"}</text>
            </Show>
            <Show when={props.loading && props.transactions.length > 0}>
              <text style={textStyle({ dim: true })}>{"  Loading..."}</text>
            </Show>
          </scrollbox>
        </box>
      </Show>
    </Show>
  );
}
