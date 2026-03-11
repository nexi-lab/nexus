/**
 * Reputation view: scores list and leaderboard table side by side.
 */

import React from "react";
import type { ReputationScore, LeaderboardEntry } from "../../stores/access-store.js";

interface ReputationViewProps {
  readonly scores: readonly ReputationScore[];
  readonly scoresLoading: boolean;
  readonly leaderboard: readonly LeaderboardEntry[];
  readonly leaderboardLoading: boolean;
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function renderScoreBar(score: number, width: number): string {
  const clamped = Math.max(0, Math.min(100, score));
  const filled = Math.round((clamped / 100) * width);
  const empty = width - filled;
  return `[${"#".repeat(filled)}${"-".repeat(empty)}] ${score.toFixed(0)}`;
}

export function ReputationView({
  scores,
  scoresLoading,
  leaderboard,
  leaderboardLoading,
}: ReputationViewProps): React.ReactNode {
  return (
    <box height="100%" width="100%" flexDirection="row">
      {/* Left: Scores */}
      <box width="50%" height="100%" flexDirection="column">
        <box height={1} width="100%">
          <text>--- Reputation Scores ---</text>
        </box>

        {scoresLoading ? (
          <box flexGrow={1} justifyContent="center" alignItems="center">
            <text>Loading scores...</text>
          </box>
        ) : scores.length === 0 ? (
          <box flexGrow={1} justifyContent="center" alignItems="center">
            <text>No reputation scores</text>
          </box>
        ) : (
          <scrollbox flexGrow={1} width="100%">
            {/* Scores header */}
            <box height={1} width="100%">
              <text>{"  AGENT            TRUST LEVEL    SCORE               UPDATED"}</text>
            </box>
            <box height={1} width="100%">
              <text>{"  ---------------  -------------  ------------------  -------"}</text>
            </box>

            {scores.map((s) => (
              <box key={s.agent_id} height={1} width="100%">
                <text>
                  {`  ${s.agent_id.padEnd(15)}  ${s.trust_level.padEnd(13)}  ${renderScoreBar(s.score, 12).padEnd(18)}  ${formatTimestamp(s.last_updated)}`}
                </text>
              </box>
            ))}
          </scrollbox>
        )}
      </box>

      {/* Right: Leaderboard */}
      <box width="50%" height="100%" flexDirection="column">
        <box height={1} width="100%">
          <text>--- Trust Leaderboard ---</text>
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
            {/* Leaderboard header */}
            <box height={1} width="100%">
              <text>{"  RANK  AGENT            SCORE  TRUST LEVEL"}</text>
            </box>
            <box height={1} width="100%">
              <text>{"  ----  ---------------  -----  -------------"}</text>
            </box>

            {leaderboard.map((entry) => (
              <box key={`lb-${entry.rank}`} height={1} width="100%">
                <text>
                  {`  ${String(entry.rank).padStart(4)}  ${entry.agent_id.padEnd(15)}  ${String(entry.score).padEnd(5)}  ${entry.trust_level}`}
                </text>
              </box>
            ))}
          </scrollbox>
        )}
      </box>
    </box>
  );
}
