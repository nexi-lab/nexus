import type { JSX } from "solid-js";
/**
 * Uploads tab: displays upload sessions with progress bars,
 * filename, progress (offset/length), status, and expiry.
 */

import type { UploadSession } from "../../stores/upload-store.js";

interface UploadsTabProps {
  readonly sessions: readonly UploadSession[];
  readonly selectedIndex: number;
  readonly loading: boolean;
}

function formatProgress(offset: number, length: number): string {
  if (length <= 0) return `${offset} bytes`;
  const pct = Math.min(100, Math.round((offset / length) * 100));
  const barWidth = 20;
  const filled = Math.round((pct / 100) * barWidth);
  const bar = "\u2588".repeat(filled) + "\u2591".repeat(barWidth - filled);
  return `[${bar}] ${pct}% (${offset}/${length})`;
}

export function UploadsTab(props: UploadsTabProps): JSX.Element {
  return (
    <box height="100%" width="100%" flexDirection="column">
      <text>
        {props.loading
          ? "Loading upload sessions..."
          : props.sessions.length === 0
            ? "No active upload sessions."
            : `${props.sessions.length} upload sessions`}
      </text>
      <scrollbox flexGrow={1} width="100%">
        {props.sessions.map((session, i) => {
          const isSelected = i === props.selectedIndex;
          const prefix = isSelected ? "> " : "  ";
          const filename = session.filename ?? "(unknown)";
          const progress = formatProgress(session.offset, session.length);
          const expiry = session.expires_at ? `expires ${session.expires_at}` : "no expiry";
          return (
            <box height={2} width="100%" flexDirection="column">
              <text>{`${prefix}${filename}  ${session.id.slice(0, 8)}...  ${expiry}`}</text>
              <text>{`    ${progress}`}</text>
            </box>
          );
        })}
      </scrollbox>
    </box>
  );
}
