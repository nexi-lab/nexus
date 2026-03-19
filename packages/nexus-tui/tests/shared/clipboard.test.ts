/**
 * Tests for clipboard OSC 52 support.
 * Tests the real copyToClipboard function with hardcoded expected values.
 */

import { describe, it, expect, beforeEach, afterEach } from "bun:test";
import { copyToClipboard } from "../../src/shared/lib/clipboard.js";

describe("copyToClipboard", () => {
  let writtenData: string;
  const originalWrite = process.stdout.write;

  beforeEach(() => {
    writtenData = "";
    process.stdout.write = ((chunk: string) => {
      writtenData += chunk;
      return true;
    }) as typeof process.stdout.write;
  });

  afterEach(() => {
    process.stdout.write = originalWrite;
  });

  it("writes OSC 52 sequence with correct base64 for 'hello'", () => {
    copyToClipboard("hello");
    expect(writtenData).toBe("\x1b]52;c;aGVsbG8=\x07");
  });

  it("writes correct base64 for empty string", () => {
    copyToClipboard("");
    expect(writtenData).toBe("\x1b]52;c;\x07");
  });

  it("writes correct base64 for file path", () => {
    copyToClipboard("/path/to/file.txt");
    expect(writtenData).toBe("\x1b]52;c;L3BhdGgvdG8vZmlsZS50eHQ=\x07");
  });

  it("writes correct base64 for UUID", () => {
    copyToClipboard("550e8400-e29b-41d4-a716-446655440000");
    expect(writtenData).toBe("\x1b]52;c;NTUwZTg0MDAtZTI5Yi00MWQ0LWE3MTYtNDQ2NjU1NDQwMDAw\x07");
  });

  it("writes correct base64 for multiline text", () => {
    copyToClipboard("line1\nline2");
    expect(writtenData).toBe("\x1b]52;c;bGluZTEKbGluZTI=\x07");
  });

  it("starts with OSC 52 prefix", () => {
    copyToClipboard("test");
    expect(writtenData.startsWith("\x1b]52;c;")).toBe(true);
  });

  it("ends with BEL terminator", () => {
    copyToClipboard("test");
    expect(writtenData.endsWith("\x07")).toBe(true);
  });
});
