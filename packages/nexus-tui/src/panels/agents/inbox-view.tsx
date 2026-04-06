import type { JSX } from "solid-js";
/**
 * IPC message view for an agent — inbox, processed, and dead_letter.
 */

import type { InboxMessage } from "../../stores/agents-store.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { statusColor } from "../../shared/theme.js";
import { textStyle } from "../../shared/text-style.js";

interface InboxViewProps {
  readonly messages: readonly InboxMessage[];
  readonly count: number;
  readonly processedMessages: readonly InboxMessage[];
  readonly deadLetterMessages: readonly InboxMessage[];
  readonly loading: boolean;
  readonly selectedIndex?: number;
  readonly previewContent?: string | null;
}

function parseFilename(filename: string): { label: string; ext: string } {
  const lastDot = filename.lastIndexOf(".");
  const ext = lastDot >= 0 ? filename.slice(lastDot + 1).toUpperCase() : "";
  const base = lastDot >= 0 ? filename.slice(0, lastDot) : filename;
  const label = base.replace(/[-_]/g, " ").slice(0, 40);
  return { label, ext };
}

function MessageList({ messages, emptyText }: { messages: readonly InboxMessage[]; emptyText: string }): JSX.Element {
  if (messages.length === 0) {
    return <text style={textStyle({ dim: true })}>{`  ${emptyText}`}</text>;
  }
  return (
    <>
      {messages.map((msg, i) => {
        const { label, ext } = parseFilename(msg.filename);
        const extTag = ext ? ` [${ext}]` : "";
        return (
          <box key={`msg-${i}`} height={1} width="100%">
            <text>{`  ${i + 1}. ${label}${extTag}`}</text>
          </box>
        );
      })}
    </>
  );
}

export function InboxView({ messages, count, processedMessages, deadLetterMessages, loading }: InboxViewProps): JSX.Element {
  if (loading) {
    return <LoadingIndicator message="Loading messages..." />;
  }

  const totalAll = count + processedMessages.length + deadLetterMessages.length;

  return (
    <box height="100%" width="100%" flexDirection="column">
      <scrollbox flexGrow={1} width="100%">
        {/* Inbox section */}
        <box height={1} width="100%">
          <text>
            <span style={textStyle({ fg: statusColor.info, bold: true })}>{`Inbox (${count})`}</span>
            <span style={textStyle({ dim: true })}>{" — pending messages"}</span>
          </text>
        </box>
        <MessageList messages={messages} emptyText="No pending messages" />

        {/* Processed section */}
        <text>{""}</text>
        <box height={1} width="100%">
          <text>
            <span style={textStyle({ fg: statusColor.healthy, bold: true })}>{`Processed (${processedMessages.length})`}</span>
            <span style={textStyle({ dim: true })}>{" — consumed by agent"}</span>
          </text>
        </box>
        <MessageList messages={processedMessages} emptyText="No processed messages" />

        {/* Dead letter section */}
        <text>{""}</text>
        <box height={1} width="100%">
          <text>
            <span style={textStyle({ fg: statusColor.error, bold: true })}>{`Dead Letter (${deadLetterMessages.length})`}</span>
            <span style={textStyle({ dim: true })}>{" — expired or failed"}</span>
          </text>
        </box>
        <MessageList messages={deadLetterMessages} emptyText="No dead letter messages" />

        {totalAll === 0 && (
          <>
            <text>{""}</text>
            <text style={textStyle({ dim: true })}>{"  No IPC messages. Send messages with POST /api/v2/ipc/send"}</text>
          </>
        )}
      </scrollbox>
    </box>
  );
}
