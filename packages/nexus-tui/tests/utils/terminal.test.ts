/**
 * Tests for the resetTerminal() utility.
 *
 * Covers:
 * - All five expected escape sequences are present in the output
 * - resetTerminal() writes to fd 1 (stdout) synchronously
 */

import { describe, it, expect, mock, beforeEach, afterEach } from "bun:test";
import fs from "fs";
import { TERMINAL_RESET_SEQUENCES, resetTerminal } from "../../src/utils/terminal.js";

describe("TERMINAL_RESET_SEQUENCES", () => {
  it("contains all five required sequences", () => {
    expect(TERMINAL_RESET_SEQUENCES).toContain("\x1b[?1003l"); // all-motion mouse off
    expect(TERMINAL_RESET_SEQUENCES).toContain("\x1b[?1006l"); // SGR mouse off
    expect(TERMINAL_RESET_SEQUENCES).toContain("\x1b[?1000l"); // normal mouse off
    expect(TERMINAL_RESET_SEQUENCES).toContain("\x1b[?1049l"); // exit alternate screen
    expect(TERMINAL_RESET_SEQUENCES).toContain("\x1b[?25h");   // show cursor
  });

  it("has exactly five sequences", () => {
    expect(TERMINAL_RESET_SEQUENCES.length).toBe(5);
  });
});

describe("resetTerminal", () => {
  let writtenData: string;
  let writeSyncMock: ReturnType<typeof mock>;
  let originalSetRawMode: typeof process.stdin.setRawMode | undefined;

  beforeEach(() => {
    writtenData = "";
    writeSyncMock = mock((fd: number, data: string) => {
      if (fd === 1) writtenData += data;
      return data.length;
    });
    // Spy on fs.writeSync
    (fs as any).writeSync = writeSyncMock;

    originalSetRawMode = process.stdin.setRawMode;
    (process.stdin as any).setRawMode = mock(() => {});
    (process.stdin as any).pause = mock(() => {});
  });

  afterEach(() => {
    (process.stdin as any).setRawMode = originalSetRawMode;
  });

  it("writes all five escape sequences to stdout (fd 1)", () => {
    resetTerminal();

    for (const seq of TERMINAL_RESET_SEQUENCES) {
      expect(writtenData).toContain(seq);
    }
  });

  it("writes sequences as a single concatenated call", () => {
    resetTerminal();
    expect(writeSyncMock).toHaveBeenCalledTimes(1);
    expect(writeSyncMock.mock.calls[0]?.[0]).toBe(1); // fd 1 = stdout
  });

  it("calls setRawMode(false) to stop raw input", () => {
    resetTerminal();
    expect((process.stdin.setRawMode as ReturnType<typeof mock>)).toHaveBeenCalledWith(false);
  });

  it("calls stdin.pause()", () => {
    resetTerminal();
    expect((process.stdin.pause as ReturnType<typeof mock>)).toHaveBeenCalled();
  });
});
