import type { JSX } from "solid-js";
/**
 * CommandOutput — renders streaming output from local nexus CLI commands.
 *
 * Uses StyledText for ANSI rendering and the CommandRunnerStore for state.
 * Shows a spinner while running (Decision 15A).
 */

import { useCommandRunnerStore } from "../../services/command-runner.js";
import { StyledText } from "./styled-text.js";
import { Spinner } from "./spinner.js";
import { statusColor } from "../theme.js";
import { textStyle } from "../text-style.js";

const ERROR_HINTS: ReadonlyArray<{ pattern: RegExp; hint: string }> = [
  { pattern: /authentication required|unauthorized|401/i, hint: "Check your API key (NEXUS_API_KEY or nexus.yaml api_key)" },
  { pattern: /connection refused/i, hint: "Is the server running? Try Shift+U to start it" },
  { pattern: /grpc.*unavailable|failed to connect/i, hint: "gRPC endpoint unreachable — check server logs" },
  { pattern: /timed? ?out|deadline exceeded/i, hint: "Request timed out — the server may be overloaded" },
  { pattern: /address already in use|EADDRINUSE/i, hint: "Port is already in use — stop the existing process or change ports" },
  { pattern: /permission denied|EACCES/i, hint: "Permission denied — check file/directory permissions" },
  { pattern: /no such file|ENOENT|not found/i, hint: "File or command not found — check paths and installation" },
];

function findErrorHint(lines: readonly string[]): string | null {
  const tail = lines.slice(-20);
  for (const line of tail) {
    for (const { pattern, hint } of ERROR_HINTS) {
      if (pattern.test(line)) return hint;
    }
  }
  return null;
}

export function CommandOutput(): JSX.Element {
  const status = useCommandRunnerStore((s) => s.status);
  const outputLines = useCommandRunnerStore((s) => s.outputLines);
  const commandLabel = useCommandRunnerStore((s) => s.commandLabel);
  const exitCode = useCommandRunnerStore((s) => s.exitCode);
  const spawnError = useCommandRunnerStore((s) => s.spawnError);

  if (status === "idle") {
    return null;
  }

  const output = outputLines.join("\n");

  return (
    <box flexDirection="column" width="100%">
      {/* Header */}
      <box height={1} width="100%">
        <text>
          <span style={textStyle({ dim: true })}>{"$ "}</span>
          <span style={textStyle({ bold: true })}>{commandLabel}</span>
        </text>
      </box>

      {/* Output */}
      {output ? (
        <box width="100%">
          <StyledText>{output}</StyledText>
        </box>
      ) : status === "running" ? (
        <Spinner label="Running..." />
      ) : null}

      {/* Spawn error */}
      {spawnError && (
        <box height={1} width="100%">
          <text style={textStyle({ fg: statusColor.error })}>{"Error: "}{spawnError}</text>
        </box>
      )}

      {/* Status footer */}
      {status === "success" && (
        <box height={1} width="100%">
          <text style={textStyle({ fg: statusColor.success })}>{"Command completed successfully"}</text>
        </box>
      )}
      {status === "error" && exitCode !== null && (
        <box height={1} width="100%">
          <text style={textStyle({ fg: statusColor.error })}>{`Command failed with exit code ${exitCode}`}</text>
        </box>
      )}
      {status === "error" && (() => {
        const hint = findErrorHint(outputLines);
        return hint ? (
          <box height={1} width="100%">
            <text style={textStyle({ fg: statusColor.warning })}>{`  Hint: ${hint}`}</text>
          </box>
        ) : null;
      })()}
    </box>
  );
}
