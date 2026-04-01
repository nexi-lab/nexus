/**
 * Constraint list: displays ReBAC governance constraints with
 * from/to agents, type, and creation time.
 */

import React from "react";
import type { GovernanceConstraint } from "../../stores/access-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { textStyle } from "../../shared/text-style.js";

interface ConstraintListProps {
  readonly constraints: readonly GovernanceConstraint[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function shortId(id: string): string {
  if (id.length <= 14) return id;
  return `${id.slice(0, 11)}..`;
}

function formatTime(ts: string): string {
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function ConstraintList({
  constraints,
  selectedIndex,
  loading,
}: ConstraintListProps): React.ReactNode {
  if (loading) {
    return <LoadingIndicator message="Loading constraints..." />;
  }

  if (constraints.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text style={textStyle({ dim: true })}>No governance constraints found</text>
      </box>
    );
  }

  return (
    <scrollbox height="100%" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>{"  FROM AGENT     TO AGENT       TYPE            CREATED AT"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  -------------  -------------  --------------  -----------------------"}</text>
      </box>

      {/* Rows */}
      {constraints.map((c, i) => {
        const isSelected = i === selectedIndex;
        const prefix = isSelected ? "> " : "  ";

        return (
          <box key={c.id} height={1} width="100%">
            <text>
              {`${prefix}${shortId(c.from_agent_id).padEnd(13)}  ${shortId(c.to_agent_id).padEnd(13)}  ${c.constraint_type.padEnd(14)}  ${formatTime(c.created_at)}`}
            </text>
          </box>
        );
      })}
    </scrollbox>
  );
}
