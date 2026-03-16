/**
 * Tests for CommandRunner — Issues #11A+B.
 *
 * Unit tests for allowlist, store operations, and output buffer.
 * Light integration tests with real Bun.spawn.
 */

import { describe, it, expect, beforeEach } from "bun:test";
import {
  useCommandRunnerStore,
  executeLocalCommand,
  killAllProcesses,
} from "../../src/services/command-runner.js";

describe("CommandRunner", () => {
  beforeEach(() => {
    useCommandRunnerStore.getState().reset();
  });

  // =========================================================================
  // Store unit tests
  // =========================================================================
  describe("store", () => {
    it("starts in idle state", () => {
      const state = useCommandRunnerStore.getState();
      expect(state.status).toBe("idle");
      expect(state.outputLines).toEqual([]);
      expect(state.exitCode).toBeNull();
      expect(state.commandLabel).toBe("");
      expect(state.spawnError).toBeNull();
    });

    it("appendOutput splits chunks into lines", () => {
      const { appendOutput } = useCommandRunnerStore.getState();
      appendOutput("line 1\nline 2\nline 3");
      const lines = useCommandRunnerStore.getState().outputLines;
      expect(lines).toEqual(["line 1", "line 2", "line 3"]);
    });

    it("appendOutput joins partial lines across chunks", () => {
      const { appendOutput } = useCommandRunnerStore.getState();
      appendOutput("hello ");
      appendOutput("world\ndone");
      const lines = useCommandRunnerStore.getState().outputLines;
      expect(lines).toEqual(["hello world", "done"]);
    });

    it("appendOutput windows to MAX_OUTPUT_LINES (200)", () => {
      const { appendOutput } = useCommandRunnerStore.getState();
      const bigChunk = Array.from({ length: 300 }, (_, i) => `line ${i}`).join("\n");
      appendOutput(bigChunk);
      const lines = useCommandRunnerStore.getState().outputLines;
      expect(lines.length).toBe(200);
      expect(lines[0]).toBe("line 100");
      expect(lines[199]).toBe("line 299");
    });

    it("setStatus updates status and exit code", () => {
      const { setStatus } = useCommandRunnerStore.getState();
      setStatus("running");
      expect(useCommandRunnerStore.getState().status).toBe("running");
      expect(useCommandRunnerStore.getState().exitCode).toBeNull();

      setStatus("success", 0);
      expect(useCommandRunnerStore.getState().status).toBe("success");
      expect(useCommandRunnerStore.getState().exitCode).toBe(0);
    });

    it("setSpawnError sets error status", () => {
      const { setSpawnError } = useCommandRunnerStore.getState();
      setSpawnError("nexus not found");
      const state = useCommandRunnerStore.getState();
      expect(state.status).toBe("error");
      expect(state.spawnError).toBe("nexus not found");
    });

    it("reset returns to initial state", () => {
      const store = useCommandRunnerStore.getState();
      store.appendOutput("some output");
      store.setStatus("success", 0);
      store.reset();

      const state = useCommandRunnerStore.getState();
      expect(state.status).toBe("idle");
      expect(state.outputLines).toEqual([]);
      expect(state.exitCode).toBeNull();
    });
  });

  // =========================================================================
  // Allowlist enforcement (defense-in-depth)
  // =========================================================================
  describe("allowlist", () => {
    it("rejects commands not in allowlist", () => {
      executeLocalCommand("rm", ["/important"]);
      const state = useCommandRunnerStore.getState();
      expect(state.status).toBe("error");
      expect(state.spawnError).toContain("not in the allowlist");
    });

    it("rejects migrate command", () => {
      executeLocalCommand("migrate", ["--destructive"]);
      expect(useCommandRunnerStore.getState().spawnError).toContain("not in the allowlist");
    });

    it("accepts allowed commands (init)", () => {
      // This will actually try to spawn `nexus init` which may fail,
      // but the point is it doesn't get rejected by the allowlist
      executeLocalCommand("init", ["--help"]);
      const state = useCommandRunnerStore.getState();
      expect(state.spawnError).toBeNull();
      // Status should be "running" (or "error" if nexus is not installed, but NOT allowlist error)
      expect(state.status).not.toBe("idle");
    });

    it("accepts allowed commands (build)", () => {
      executeLocalCommand("build", []);
      expect(useCommandRunnerStore.getState().spawnError).toBeNull();
    });

    it("accepts allowed commands (demo)", () => {
      executeLocalCommand("demo", ["init"]);
      expect(useCommandRunnerStore.getState().spawnError).toBeNull();
    });

    it("accepts allowed commands (brick)", () => {
      executeLocalCommand("brick", ["mount", "test"]);
      expect(useCommandRunnerStore.getState().spawnError).toBeNull();
    });

    it("accepts allowed commands (agent)", () => {
      executeLocalCommand("agent", ["spawn"]);
      expect(useCommandRunnerStore.getState().spawnError).toBeNull();
    });
  });

  // =========================================================================
  // Process management
  // =========================================================================
  describe("process management", () => {
    it("prevents concurrent commands", () => {
      // Set status to running manually
      useCommandRunnerStore.setState({ status: "running", commandLabel: "nexus init" });

      executeLocalCommand("build", []);

      // Should still be the original command
      expect(useCommandRunnerStore.getState().commandLabel).toBe("nexus init");
    });

    it("killAllProcesses does not throw when no processes running", () => {
      expect(() => killAllProcesses()).not.toThrow();
    });
  });

  // =========================================================================
  // Integration tests (11B): real Bun.spawn
  // =========================================================================
  describe("integration: real process spawn", () => {
    it("captures output from a real command", async () => {
      // Use echo which is guaranteed to exist
      const proc = Bun.spawn(["echo", "hello world"], { stdout: "pipe" });
      const output = await new Response(proc.stdout).text();
      expect(output.trim()).toBe("hello world");
      await proc.exited;
    });

    it("captures exit code from failing command", async () => {
      const proc = Bun.spawn(["false"], { stdout: "pipe", stderr: "pipe" });
      const exitCode = await proc.exited;
      expect(exitCode).not.toBe(0);
    });

    it("streams output incrementally", async () => {
      // Spawn a process that produces output over time
      const proc = Bun.spawn(["printf", "line1\\nline2\\nline3\\n"], {
        stdout: "pipe",
      });

      const chunks: string[] = [];
      const reader = proc.stdout.getReader();
      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(decoder.decode(value, { stream: true }));
      }

      await proc.exited;
      const fullOutput = chunks.join("");
      expect(fullOutput).toContain("line1");
      expect(fullOutput).toContain("line2");
      expect(fullOutput).toContain("line3");
    });
  });
});
