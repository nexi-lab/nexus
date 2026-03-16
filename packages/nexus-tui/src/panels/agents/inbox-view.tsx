/**
 * IPC inbox message list for an agent.
 */

import React from "react";
import type { InboxMessage } from "../../stores/agents-store.js";

interface InboxViewProps {
  readonly messages: readonly InboxMessage[];
  readonly count: number;
  readonly loading: boolean;
}

export function InboxView({ messages, count, loading }: InboxViewProps): React.ReactNode {
  if (loading) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>Loading inbox...</text>
      </box>
    );
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
        <scrollbox flexGrow={1} width="100%">
          {messages.map((msg, i) => (
            <box key={`msg-${i}`} height={1} width="100%">
              <text>{`  ${i + 1}. ${msg.filename}`}</text>
            </box>
          ))}
        </scrollbox>
      )}
    </box>
  );
}
