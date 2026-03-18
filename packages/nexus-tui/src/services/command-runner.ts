/**
 * CommandRunner — executes local `nexus` CLI subcommands via Bun.spawn().
 *
 * Decisions implemented:
 *   2A: Shell out to the Python `nexus` binary
 *   4A: Strict allowlist (validated in parseCommand, enforced here as defense-in-depth)
 *   6A: Process lifecycle management with cleanup on shutdown
 *   7A: Accumulator buffer for streaming output
 *   13A+C: Windowed rendering (last MAX_OUTPUT_LINES) + throttled state updates
 *   15A: Show spinner immediately (handled by consumer component)
 */

import { create } from "zustand";

// =============================================================================
// Constants
// =============================================================================

/** Maximum lines retained in the output buffer (Decision 13A). */
const MAX_OUTPUT_LINES = 200;

/** Minimum interval between state updates in ms (Decision 13C). */
const THROTTLE_MS = 100;

/** Defense-in-depth: re-validate the subcommand even though parseCommand already checks. */
const ALLOWED_COMMANDS = new Set(["init", "build", "demo", "brick", "agent", "up"]);

// =============================================================================
// Types
// =============================================================================

export type CommandStatus = "idle" | "running" | "success" | "error";

export interface CommandRunnerState {
  /** Current command status. */
  readonly status: CommandStatus;
  /** Output lines (windowed to last MAX_OUTPUT_LINES). */
  readonly outputLines: readonly string[];
  /** Exit code of the last command (null while running). */
  readonly exitCode: number | null;
  /** The command string being/was executed. */
  readonly commandLabel: string;
  /** Error message if the command failed to spawn. */
  readonly spawnError: string | null;
}

export interface CommandRunnerStore extends CommandRunnerState {
  readonly appendOutput: (chunk: string) => void;
  readonly setStatus: (status: CommandStatus, exitCode?: number | null) => void;
  readonly setSpawnError: (error: string) => void;
  readonly reset: () => void;
}

// =============================================================================
// Store
// =============================================================================

const INITIAL_STATE: CommandRunnerState = {
  status: "idle",
  outputLines: [],
  exitCode: null,
  commandLabel: "",
  spawnError: null,
};

export const useCommandRunnerStore = create<CommandRunnerStore>((set) => ({
  ...INITIAL_STATE,

  appendOutput: (chunk) => {
    set((state) => {
      // Split chunk into lines, preserving partial last line
      const newLines = chunk.split("\n");
      const combined = [...state.outputLines];

      // Append first fragment to the last existing line (handles partial lines)
      if (combined.length > 0 && newLines.length > 0) {
        combined[combined.length - 1] = combined[combined.length - 1]! + newLines[0]!;
        newLines.shift();
      }

      combined.push(...newLines);

      // Window to last MAX_OUTPUT_LINES (Decision 13A)
      const windowed = combined.length > MAX_OUTPUT_LINES
        ? combined.slice(-MAX_OUTPUT_LINES)
        : combined;

      return { outputLines: windowed };
    });
  },

  setStatus: (status, exitCode) => {
    set({ status, exitCode: exitCode ?? null });
  },

  setSpawnError: (error) => {
    set({ status: "error", spawnError: error });
  },

  reset: () => {
    set(INITIAL_STATE);
  },
}));

// =============================================================================
// Process management (Decision 6A)
// =============================================================================

/** Set of currently running child processes for cleanup on shutdown. */
const activeProcesses = new Set<{ kill(): void }>();

/**
 * Kill all running child processes. Called from the shutdown handler.
 */
export function killAllProcesses(): void {
  for (const proc of activeProcesses) {
    try {
      proc.kill();
    } catch {
      // Process may have already exited
    }
  }
  activeProcesses.clear();
}

// =============================================================================
// Execute local command
// =============================================================================

/**
 * Execute a local nexus subcommand via Bun.spawn().
 *
 * Output is streamed into the CommandRunnerStore for rendering by CommandOutput.
 */
export function executeLocalCommand(command: string, args: readonly string[]): void {
  // Defense-in-depth: re-validate allowlist (already checked in parseCommand)
  if (!ALLOWED_COMMANDS.has(command)) {
    useCommandRunnerStore.getState().setSpawnError(
      `Command "${command}" is not in the allowlist. Allowed: ${[...ALLOWED_COMMANDS].join(", ")}`,
    );
    return;
  }

  const store = useCommandRunnerStore.getState();

  // Don't start a new command if one is already running
  if (store.status === "running") {
    return;
  }

  // Reset state
  useCommandRunnerStore.setState({
    ...INITIAL_STATE,
    status: "running",
    commandLabel: `nexus ${command} ${args.join(" ")}`.trim(),
  });

  // Prefer .venv/bin/nexus (project venv) over system PATH to avoid picking up
  // stale installs (e.g. /opt/anaconda3/bin/nexus which lacks the `up` command).
  // Walk up from CWD to find .venv/bin/nexus (TUI may run from packages/nexus-tui/).
  const path = require("node:path");
  const nodeFs = require("node:fs");
  let nexusBin = "nexus";
  let searchDir = process.cwd();
  for (let i = 0; i < 5; i++) {
    const candidate = path.join(searchDir, ".venv", "bin", "nexus");
    if (nodeFs.existsSync(candidate)) {
      nexusBin = candidate;
      break;
    }
    const parent = path.dirname(searchDir);
    if (parent === searchDir) break;
    searchDir = parent;
  }
  const fullArgs = [nexusBin, command, ...args];

  // Read the TUI's own nexus.yaml (in CWD) to pass NEXUS_URL and NEXUS_API_KEY
  // to subcommands like `nexus demo init`.
  const spawnEnv = { ...process.env };
  try {
    const fs = require("node:fs");
    const yaml = fs.readFileSync("nexus.yaml", "utf-8") as string;
    const portMatch = yaml.match(/ports:\s*\n(?:\s+\w+:[^\n]*\n)*?\s+http:\s*(\d+)/);
    const keyMatch = yaml.match(/^api_key:\s*["']?([^"'\n]+)["']?/m);
    if (portMatch?.[1] && !spawnEnv.NEXUS_URL) {
      spawnEnv.NEXUS_URL = `http://localhost:${portMatch[1]}`;
    }
    if (keyMatch?.[1] && !spawnEnv.NEXUS_API_KEY) {
      spawnEnv.NEXUS_API_KEY = keyMatch[1];
    }
  } catch {
    // nexus.yaml not found yet (will be created by nexus init)
  }

  try {
    // Commands run from CWD so each TUI instance gets its own nexus.yaml
    const proc = Bun.spawn(fullArgs, {
      stdout: "pipe",
      stderr: "pipe",
      env: spawnEnv,
    });

    activeProcesses.add(proc);

    // Throttled output flushing (Decision 13C)
    let pendingChunks = "";
    let flushTimer: ReturnType<typeof setTimeout> | null = null;

    function flushOutput(): void {
      if (pendingChunks) {
        useCommandRunnerStore.getState().appendOutput(pendingChunks);
        pendingChunks = "";
      }
      flushTimer = null;
    }

    function bufferChunk(text: string): void {
      pendingChunks += text;
      if (!flushTimer) {
        flushTimer = setTimeout(flushOutput, THROTTLE_MS);
      }
    }

    // Stream stdout
    (async () => {
      try {
        const reader = proc.stdout.getReader();
        const decoder = new TextDecoder();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          bufferChunk(decoder.decode(value, { stream: true }));
        }
      } catch {
        // Stream closed
      }
    })();

    // Stream stderr (interleaved with stdout)
    (async () => {
      try {
        const reader = proc.stderr.getReader();
        const decoder = new TextDecoder();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          bufferChunk(decoder.decode(value, { stream: true }));
        }
      } catch {
        // Stream closed
      }
    })();

    // Wait for process to complete
    proc.exited.then((exitCode) => {
      activeProcesses.delete(proc);
      // Flush any remaining buffered output
      if (flushTimer) {
        clearTimeout(flushTimer);
      }
      flushOutput();

      useCommandRunnerStore.getState().setStatus(
        exitCode === 0 ? "success" : "error",
        exitCode,
      );
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to spawn command";

    // Common case: `nexus` binary not found
    if (message.includes("ENOENT") || message.includes("not found")) {
      useCommandRunnerStore.getState().setSpawnError(
        `"nexus" command not found on PATH. Install the Nexus CLI: pip install nexus`,
      );
    } else {
      useCommandRunnerStore.getState().setSpawnError(message);
    }
  }
}
