/**
 * Playbook list: displays playbook records with name, scope, tags, usage, and success rate.
 */

import React from "react";
import type { PlaybookRecord } from "../../stores/search-store.js";

interface PlaybookListProps {
  readonly playbooks: readonly PlaybookRecord[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function truncateText(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return `${text.slice(0, maxLen - 3)}...`;
}

function formatRate(rate: number | null): string {
  if (rate === null || rate === undefined) return "-";
  return `${(rate * 100).toFixed(0)}%`;
}

export function PlaybookList({
  playbooks,
  selectedIndex,
  loading,
}: PlaybookListProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading playbooks...</text>
      </box>
    );
  }

  if (playbooks.length === 0) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No playbooks found</text>
      </box>
    );
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={1} width="100%">
        <text>{`Playbooks: ${playbooks.length}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  NAME                          SCOPE      VIS     VER  USED  RATE"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ----------------------------  ---------  ------  ---  ----  ----"}</text>
      </box>

      {/* Rows */}
      <scrollbox flexGrow={1} width="100%">
        {playbooks.map((p, i) => {
          const isSelected = i === selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const name = truncateText(p.name, 28).padEnd(28);
          const scope = truncateText(p.scope, 9).padEnd(9);
          const vis = truncateText(p.visibility, 6).padEnd(6);
          const ver = String(p.version).padEnd(3);
          const used = String(p.usage_count).padEnd(4);
          const rate = formatRate(p.success_rate);

          return (
            <box key={p.playbook_id} height={1} width="100%">
              <text>
                {`${prefix}${name}  ${scope}  ${vis}  ${ver}  ${used}  ${rate}`}
              </text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
