/**
 * Reputation view: leaderboard table showing agent reputation scores.
 */

import React from "react";
import type { LeaderboardEntry } from "../../stores/access-store.js";

interface ReputationViewProps {
  readonly leaderboard: readonly LeaderboardEntry[];
  readonly leaderboardLoading: boolean;
}

function shortId(id: string): string {
  if (id.length <= 16) return id;
  return `${id.slice(0, 12)}..`;
}

function renderScoreBar(score: number, width: number): string {
  const clamped = Math.max(0, Math.min(1, score));
  const filled = Math.round(clamped * width);
  const empty = width - filled;
  return `[${"#".repeat(filled)}${"-".repeat(empty)}] ${score.toFixed(2)}`;
}

export function ReputationView({
  leaderboard,
  leaderboardLoading,
}: ReputationViewProps): React.ReactNode {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>--- Reputation Leaderboard ---</text>
      </box>

      {leaderboardLoading ? (
        <box flexGrow={1} justifyContent="center" alignItems="center">
          <text>Loading leaderboard...</text>
        </box>
      ) : leaderboard.length === 0 ? (
        <box flexGrow={1} justifyContent="center" alignItems="center">
          <text>No leaderboard entries</text>
        </box>
      ) : (
        <scrollbox flexGrow={1} width="100%">
          {/* Header */}
          <box height={1} width="100%">
            <text>{"  AGENT            SCORE               CONFIDENCE  INTERACTIONS  ZONE"}</text>
          </box>
          <box height={1} width="100%">
            <text>{"  ---------------  ------------------  ----------  ------------  ---------------"}</text>
          </box>

          {leaderboard.map((entry) => (
            <box key={`lb-${entry.agent_id}-${entry.zone_id}`} height={1} width="100%">
              <text>
                {`  ${shortId(entry.agent_id).padEnd(15)}  ${renderScoreBar(entry.composite_score, 10).padEnd(18)}  ${entry.composite_confidence.toFixed(2).padEnd(10)}  ${String(entry.total_interactions).padEnd(12)}  ${shortId(entry.zone_id)}`}
              </text>
            </box>
          ))}
        </scrollbox>
      )}
    </box>
  );
}
