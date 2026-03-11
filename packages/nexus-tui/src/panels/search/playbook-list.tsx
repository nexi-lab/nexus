/**
 * Playbook list: name, description, steps count, status, last_run.
 */

import React from "react";
import type { Playbook } from "../../stores/search-store.js";

interface PlaybookListProps {
  readonly playbooks: readonly Playbook[];
  readonly loading: boolean;
}

const STATUS_BADGES: Readonly<Record<Playbook["status"], string>> = {
  active: "[ACT]",
  draft: "[DFT]",
  archived: "[ARC]",
};

function formatLastRun(ts: string | null): string {
  if (!ts) return "never";
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

export function PlaybookList({
  playbooks,
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
        <text>{"  STATUS  NAME                   STEPS  DESCRIPTION                    LAST RUN"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ------  ---------------------  -----  -----------------------------  --------"}</text>
      </box>

      {/* Rows */}
      <scrollbox flexGrow={1} width="100%">
        {playbooks.map((pb) => {
          const badge = STATUS_BADGES[pb.status] ?? `[${pb.status.toUpperCase()}]`;
          const name = pb.name.length > 21
            ? `${pb.name.slice(0, 18)}...`
            : pb.name;
          const desc = pb.description.length > 29
            ? `${pb.description.slice(0, 26)}...`
            : pb.description;

          return (
            <box key={pb.playbook_id} height={1} width="100%">
              <text>
                {`  ${badge}  ${name.padEnd(21)}  ${String(pb.steps).padEnd(5)}  ${desc.padEnd(29)}  ${formatLastRun(pb.last_run)}`}
              </text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
