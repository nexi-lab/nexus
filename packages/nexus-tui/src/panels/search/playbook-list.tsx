import type { JSX } from "solid-js";
/**
 * Playbook list: displays playbook records with name, scope, tags, usage, and success rate.
 */

import type { PlaybookRecord } from "../../stores/search-store.js";
import { truncateText } from "../../shared/utils/format-text.js";

interface PlaybookListProps {
  readonly playbooks: readonly PlaybookRecord[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function formatRate(rate: number | null): string {
  if (rate === null || rate === undefined) return "-";
  return `${(rate * 100).toFixed(0)}%`;
}

export function PlaybookList(props: PlaybookListProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading playbooks..."
          : props.playbooks.length === 0
            ? "No playbooks found"
            : `Playbooks: ${props.playbooks.length}`}
      </text>

      {/* Header */}
      <box height={1} width="100%">
        <text>{"  NAME                          SCOPE      VIS     VER  USED  RATE"}</text>
      </box>
      <box height={1} width="100%">
        <text>{"  ----------------------------  ---------  ------  ---  ----  ----"}</text>
      </box>

      {/* Rows */}
      <scrollbox flexGrow={1} width="100%">
        {props.playbooks.map((p, i) => {
          const isSelected = i === props.selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const name = truncateText(p.name, 28).padEnd(28);
          const scope = truncateText(p.scope, 9).padEnd(9);
          const vis = truncateText(p.visibility, 6).padEnd(6);
          const ver = String(p.version).padEnd(3);
          const used = String(p.usage_count).padEnd(4);
          const rate = formatRate(p.success_rate);

          return (
            <box height={1} width="100%">
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
