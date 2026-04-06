import type { JSX } from "solid-js";
/**
 * Fraud score list: displays per-agent fraud scores with component breakdown.
 * Data from GET /api/v2/governance/fraud-scores.
 */

import type { FraudScore } from "../../stores/access-store.js";

interface FraudScoreViewProps {
  readonly scores: readonly FraudScore[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function shortId(id: string): string {
  if (id.length <= 16) return id;
  return `${id.slice(0, 12)}..`;
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function FraudScoreView({
  scores,
  selectedIndex,
  loading,
}: FraudScoreViewProps): JSX.Element {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading fraud scores...</text>
      </box>
    );
  }

  if (scores.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No fraud scores. Press 'c' to compute scores for this zone.</text>
      </box>
    );
  }

  const selected = scores[selectedIndex];

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={1} width="100%">
        <text>{`Fraud Scores: ${scores.length} agents`}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  AGENT            ZONE             SCORE   COMPUTED"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ---------------  ---------------  ------  ------------------"}</text>
      </box>

      {/* Rows */}
      <scrollbox flexGrow={1} width="100%">
        {scores.map((s, i) => {
          const isSelected = i === selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const scoreStr = s.score.toFixed(3).padEnd(6);

          return (
            <box key={`${s.agent_id}-${s.zone_id}`} height={1} width="100%">
              <text>
                {`${prefix}${shortId(s.agent_id).padEnd(15)}  ${shortId(s.zone_id).padEnd(15)}  ${scoreStr}  ${formatTimestamp(s.computed_at)}`}
              </text>
            </box>
          );
        })}
      </scrollbox>

      {/* Selected score component breakdown */}
      {selected && Object.keys(selected.components).length > 0 && (
        <box height={4} width="100%" flexDirection="column">
          <text>{`Components for ${shortId(selected.agent_id)}:`}</text>
          <text>
            {Object.entries(selected.components)
              .map(([k, v]) => `  ${k}=${typeof v === "number" ? v.toFixed(3) : v}`)
              .join("  ")}
          </text>
        </box>
      )}
    </box>
  );
}
