/**
 * CommandOutput — renders streaming output from local nexus CLI commands.
 *
 * Uses StyledText for ANSI rendering and the CommandRunnerStore for state.
 * Shows a spinner while running (Decision 15A).
 */

import React from "react";
import { useCommandRunnerStore } from "../../services/command-runner.js";
import { StyledText } from "./styled-text.js";
import { Spinner } from "./spinner.js";
import { statusColor } from "../theme.js";

export function CommandOutput(): React.ReactNode {
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
          <span dimColor>{"$ "}</span>
          <span bold>{commandLabel}</span>
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
          <text foregroundColor={statusColor.error}>{"Error: "}{spawnError}</text>
        </box>
      )}

      {/* Status footer */}
      {status === "success" && (
        <box height={1} width="100%">
          <text foregroundColor={statusColor.success}>{"Command completed successfully"}</text>
        </box>
      )}
      {status === "error" && exitCode !== null && (
        <box height={1} width="100%">
          <text foregroundColor={statusColor.error}>{`Command failed with exit code ${exitCode}`}</text>
        </box>
      )}
    </box>
  );
}
