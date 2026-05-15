import type { JSX } from "solid-js";
/**
 * Drift view: shows the global drift reconciliation report
 * from GET /api/v2/bricks/drift.
 *
 * Displays: total_bricks, drifted count, actions_taken, errors,
 * last_reconcile_at, reconcile_count, and a table of drifted items.
 */

import type { DriftReportResponse } from "../../stores/zones-store.js";

interface DriftViewProps {
  readonly drift: DriftReportResponse | null;
  readonly loading: boolean;
}

function formatEpoch(epoch: number | null): string {
  if (epoch === null) return "never";
  try {
    return new Date(epoch * 1000).toLocaleString();
  } catch {
    return String(epoch);
  }
}

export function DriftView(props: DriftViewProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading drift report..."
          : !props.drift
            ? "No drift data available"
            : "--- Drift Reconciliation Report ---"}
      </text>

      {(() => {
        if (props.loading || !props.drift) return null;
        const drift = props.drift;
        const hasDrifts = drift.drifts.length > 0;

        return (
          <scrollbox flexGrow={1} width="100%">
            <box height={1} width="100%">
              <text>{`Total bricks:     ${drift.total_bricks}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{`Drifted:          ${drift.drifted}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{`Actions taken:    ${drift.actions_taken}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{`Errors:           ${drift.errors}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{`Reconcile count:  ${drift.reconcile_count}`}</text>
            </box>
            <box height={1} width="100%">
              <text>{`Last reconcile:   ${formatEpoch(drift.last_reconcile_at)}`}</text>
            </box>

            {hasDrifts && (
              <>
                <box height={1} width="100%" marginTop={1}>
                  <text>--- Drifted Items ---</text>
                </box>
                <box height={1} width="100%">
                  <text>{"  BRICK NAME         SPEC STATE   ACTUAL STATE  ACTION       DETAIL"}</text>
                </box>
                <box height={1} width="100%">
                  <text>{"  -----------------  -----------  ------------  -----------  -------------------------"}</text>
                </box>
                {drift.drifts.map((item, i) => (
                  <box key={`drift-${i}`} height={1} width="100%">
                    <text>
                      {`  ${item.brick_name.padEnd(17)}  ${item.spec_state.padEnd(11)}  ${item.actual_state.padEnd(12)}  ${item.action.padEnd(11)}  ${item.detail}`}
                    </text>
                  </box>
                ))}
              </>
            )}

            {!hasDrifts && (
              <box height={1} width="100%" marginTop={1}>
                <text>No drift detected - all bricks in spec.</text>
              </box>
            )}
          </scrollbox>
        );
      })()}
    </box>
  );
}
