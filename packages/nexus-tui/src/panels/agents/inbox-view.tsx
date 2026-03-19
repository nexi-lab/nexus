/**
 * IPC inbox message list for an agent.
 *
 * Shows each message with its filename parsed into a readable format:
 * sender, timestamp hint, and content type extracted from the filename.
 */

import React from "react";
import type { InboxMessage } from "../../stores/agents-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";

interface InboxViewProps {
  readonly messages: readonly InboxMessage[];
  readonly count: number;
  readonly loading: boolean;
  readonly selectedIndex?: number;
  readonly previewContent?: string | null;
}

/**
 * Parse a message filename into display-friendly parts.
 * Common patterns: "msg-<id>.json", "<sender>-<timestamp>.json", "<uuid>.cbor"
 */
function parseFilename(filename: string): { label: string; ext: string } {
  const lastDot = filename.lastIndexOf(".");
  const ext = lastDot >= 0 ? filename.slice(lastDot + 1).toUpperCase() : "";
  const base = lastDot >= 0 ? filename.slice(0, lastDot) : filename;
  // Convert dashes/underscores to spaces for readability, truncate long names
  const label = base.replace(/[-_]/g, " ").slice(0, 40);
  return { label, ext };
}

export function InboxView({ messages, count, loading, selectedIndex, previewContent }: InboxViewProps): React.ReactNode {
  if (loading) {
    return <LoadingIndicator message="Loading inbox..." />;
  }

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Header */}
      <box height={1} width="100%">
        <text>{`Inbox: ${count} message${count === 1 ? "" : "s"}`}</text>
      </box>

      {/* Message list */}
      {messages.length === 0 ? (
        <box flexGrow={1} justifyContent="center" alignItems="center">
          <text>No messages in inbox</text>
        </box>
      ) : (
        <box flexGrow={1} flexDirection="column">
          <scrollbox flexGrow={previewContent != null ? 0 : 1} width="100%">
            {messages.map((msg, i) => {
              const { label, ext } = parseFilename(msg.filename);
              const isSelected = i === (selectedIndex ?? -1);
              const prefix = isSelected ? "> " : "  ";
              const extTag = ext ? ` [${ext}]` : "";
              return (
                <box key={`msg-${i}`} height={1} width="100%">
                  <text>{`${prefix}${i + 1}. ${label}${extTag}`}</text>
                </box>
              );
            })}
          </scrollbox>

          {/* Preview pane for selected message */}
          {previewContent != null && (
            <box height={4} width="100%" borderStyle="single" flexDirection="column">
              <box height={1} width="100%">
                <text>--- Preview ---</text>
              </box>
              <box flexGrow={1} width="100%">
                <text>{previewContent.slice(0, 200) || "(empty)"}</text>
              </box>
            </box>
          )}
        </box>
      )}
    </box>
  );
}
