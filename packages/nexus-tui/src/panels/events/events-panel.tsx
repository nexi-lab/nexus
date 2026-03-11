/**
 * Real-time SSE event stream viewer.
 */

import React, { useEffect } from "react";
import { useEventsStore } from "../../stores/events-store.js";
import { useGlobalStore } from "../../stores/global-store.js";

export default function EventsPanel(): React.ReactNode {
  const config = useGlobalStore((s) => s.config);
  const connected = useEventsStore((s) => s.connected);
  const events = useEventsStore((s) => s.filteredEvents);
  const reconnectCount = useEventsStore((s) => s.reconnectCount);
  const connect = useEventsStore((s) => s.connect);
  const disconnect = useEventsStore((s) => s.disconnect);

  // Auto-connect on mount
  useEffect(() => {
    if (config.apiKey && config.baseUrl) {
      connect(config.baseUrl, config.apiKey);
    }
    return () => disconnect();
  }, [config.apiKey, config.baseUrl, connect, disconnect]);

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Status bar */}
      <box height={1} width="100%">
        <text>
          {connected
            ? `● Connected — ${events.length} events`
            : reconnectCount > 0
              ? `◐ Reconnecting (attempt ${reconnectCount})...`
              : "○ Disconnected"}
        </text>
      </box>

      {/* Event stream */}
      <scrollbox flexGrow={1} width="100%">
        {events.length === 0 ? (
          <text>Waiting for events...</text>
        ) : (
          events.map((event, index) => (
            <box key={event.id ?? index} height={1} width="100%" flexDirection="row">
              <text>{`[${event.event}] ${truncate(event.data, 120)}`}</text>
            </box>
          ))
        )}
      </scrollbox>

      {/* Help */}
      <box height={1} width="100%">
        <text>{"c:clear  f:filter  r:reconnect  q:back"}</text>
      </box>
    </box>
  );
}

function truncate(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str;
  return str.slice(0, maxLen - 3) + "...";
}
