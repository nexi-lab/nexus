/**
 * Displays the API response with syntax highlighting and status info.
 */

import React from "react";
import { useApiConsoleStore } from "../../stores/api-console-store.js";
import { httpStatusColor } from "../../shared/theme.js";
import { StyledText } from "../../shared/components/styled-text.js";

export function ResponseViewer(): React.ReactNode {
  const response = useApiConsoleStore((s) => s.response);

  if (!response) {
    return (
      <box height="100%" width="100%" justifyContent="center" alignItems="center">
        <text>No response yet</text>
      </box>
    );
  }

  if (response.error) {
    return (
      <box height="100%" width="100%" flexDirection="column">
        <text>{`Error: ${response.error}`}</text>
        <text>{`Time: ${response.timeMs.toFixed(0)}ms`}</text>
      </box>
    );
  }

  const statusCategory = Math.floor(response.status / 100);
  const statusPrefix = statusCategory === 2 ? "✓" : statusCategory === 4 ? "✗" : "!";

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Status line */}
      <box height={1} width="100%">
        <text>{`${statusPrefix} `}</text>
        <text foregroundColor={httpStatusColor(response.status)}>{`${response.status}`}</text>
        <text>{` ${response.statusText} — ${response.timeMs.toFixed(0)}ms`}</text>
      </box>

      {/* Response body with syntax highlighting (ANSI-aware) */}
      <scrollbox flexGrow={1} width="100%">
        {response.body.includes("\x1b[") ? (
          <StyledText>{response.body}</StyledText>
        ) : (
          <code content={response.body} filetype="json" syntaxStyle={undefined!} />
        )}
      </scrollbox>
    </box>
  );
}
