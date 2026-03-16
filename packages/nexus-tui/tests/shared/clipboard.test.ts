import { describe, it, expect, mock, beforeEach, afterEach } from "bun:test";
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

  it("writes correct OSC 52 sequence for simple text", () => {
    copyToClipboard("hello");
    const encoded = Buffer.from("hello").toString("base64");
    expect(writtenData).toBe(`\x1b]52;c;${encoded}\x07`);
  });

  it("encodes base64 correctly for simple ASCII", () => {
    copyToClipboard("hello");
    // "hello" in base64 is "aGVsbG8="
    expect(writtenData).toBe(`\x1b]52;c;aGVsbG8=\x07`);
  });

  it("handles empty string", () => {
    copyToClipboard("");
    const encoded = Buffer.from("").toString("base64");
    expect(writtenData).toBe(`\x1b]52;c;${encoded}\x07`);
  });

  it("handles unicode text", () => {
    copyToClipboard("cafe\u0301");
    const encoded = Buffer.from("cafe\u0301").toString("base64");
    expect(writtenData).toBe(`\x1b]52;c;${encoded}\x07`);
  });

  it("handles text with special characters", () => {
    copyToClipboard("/path/to/file.txt");
    const encoded = Buffer.from("/path/to/file.txt").toString("base64");
    expect(writtenData).toBe(`\x1b]52;c;${encoded}\x07`);
  });

  it("handles multiline text", () => {
    copyToClipboard("line1\nline2\nline3");
    const encoded = Buffer.from("line1\nline2\nline3").toString("base64");
    expect(writtenData).toBe(`\x1b]52;c;${encoded}\x07`);
  });

  it("handles long strings (UUIDs, IDs)", () => {
    const uuid = "550e8400-e29b-41d4-a716-446655440000";
    copyToClipboard(uuid);
    const encoded = Buffer.from(uuid).toString("base64");
    expect(writtenData).toBe(`\x1b]52;c;${encoded}\x07`);
  });
});
