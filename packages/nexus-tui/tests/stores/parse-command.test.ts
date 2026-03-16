/**
 * Tests for parseCommand() — Issues #9A, #8A.
 *
 * Covers the discriminated union (HttpCommand | LocalCommand) parser.
 */

import { describe, it, expect } from "bun:test";
import { parseCommand, type HttpCommand, type LocalCommand } from "../../src/stores/api-console-store.js";

/** Type-narrowing helper for HTTP commands. */
function expectHttp(input: string): HttpCommand {
  const result = parseCommand(input);
  expect(result).not.toBeNull();
  expect(result!.type).toBe("http");
  return result as HttpCommand;
}

/** Type-narrowing helper for local commands. */
function expectLocal(input: string): LocalCommand {
  const result = parseCommand(input);
  expect(result).not.toBeNull();
  expect(result!.type).toBe("local");
  return result as LocalCommand;
}

describe("parseCommand", () => {
  // =========================================================================
  // Edge cases: empty / whitespace / unparseable
  // =========================================================================
  describe("returns null for invalid input", () => {
    it("empty string", () => {
      expect(parseCommand("")).toBeNull();
    });

    it("whitespace only", () => {
      expect(parseCommand("   ")).toBeNull();
    });

    it("single word (no space)", () => {
      expect(parseCommand("hello")).toBeNull();
    });

    it("unknown command", () => {
      expect(parseCommand("foobar /path")).toBeNull();
    });

    it("just a slash", () => {
      expect(parseCommand("/ something")).toBeNull();
    });

    it("! with no command", () => {
      expect(parseCommand("!")).toBeNull();
    });

    it("! with only whitespace", () => {
      expect(parseCommand("!   ")).toBeNull();
    });

    it("! with disallowed command", () => {
      expect(parseCommand("!rm /important")).toBeNull();
    });

    it("! with disallowed command (migrate)", () => {
      expect(parseCommand("!migrate --destructive")).toBeNull();
    });
  });

  // =========================================================================
  // CLI shortcuts: ls, cat, stat, rm, mkdir → HttpCommand
  // =========================================================================
  describe("CLI shortcuts (type: http)", () => {
    it("ls /workspace", () => {
      const result = expectHttp("ls /workspace");
      expect(result.method).toBe("GET");
      expect(result.path).toBe("/api/v2/files/list?path=%2Fworkspace");
      expect(result.body).toBe("");
    });

    it("ls with path containing spaces", () => {
      const result = expectHttp("ls /my folder/file");
      expect(result.path).toContain(encodeURIComponent("/my folder/file"));
    });

    it("cat /file.txt", () => {
      const result = expectHttp("cat /file.txt");
      expect(result.method).toBe("GET");
      expect(result.path).toBe("/api/v2/files/read?path=%2Ffile.txt");
      expect(result.body).toBe("");
    });

    it("stat /file.txt", () => {
      const result = expectHttp("stat /file.txt");
      expect(result.method).toBe("GET");
      expect(result.path).toBe("/api/v2/files/metadata?path=%2Ffile.txt");
    });

    it("rm /file.txt", () => {
      const result = expectHttp("rm /file.txt");
      expect(result.method).toBe("DELETE");
      expect(result.path).toBe("/api/v2/files?path=%2Ffile.txt");
    });

    it("mkdir /new-dir", () => {
      const result = expectHttp("mkdir /new-dir");
      expect(result.method).toBe("POST");
      expect(result.path).toBe("/api/v2/files/mkdir");
      expect(result.body).toBe(JSON.stringify({ path: "/new-dir" }));
    });

    it("handles leading/trailing whitespace", () => {
      const result = expectHttp("  ls /workspace  ");
      expect(result.method).toBe("GET");
    });
  });

  // =========================================================================
  // Raw HTTP methods → HttpCommand
  // =========================================================================
  describe("raw HTTP methods (type: http)", () => {
    it("GET /api/v2/health", () => {
      const result = expectHttp("GET /api/v2/health");
      expect(result.method).toBe("GET");
      expect(result.path).toBe("/api/v2/health");
      expect(result.body).toBe("");
    });

    it("POST with JSON body", () => {
      const result = expectHttp('POST /api/v2/files/write {"path": "/test.txt", "content": "hello"}');
      expect(result.method).toBe("POST");
      expect(result.path).toBe("/api/v2/files/write");
      expect(result.body).toBe('{"path": "/test.txt", "content": "hello"}');
    });

    it("PUT without body", () => {
      const result = expectHttp("PUT /api/v2/files/touch");
      expect(result.method).toBe("PUT");
      expect(result.path).toBe("/api/v2/files/touch");
      expect(result.body).toBe("");
    });

    it("PATCH with body", () => {
      const result = expectHttp('PATCH /api/v2/agents/1 {"name": "updated"}');
      expect(result.method).toBe("PATCH");
      expect(result.body).toBe('{"name": "updated"}');
    });

    it("DELETE path", () => {
      const result = expectHttp("DELETE /api/v2/files?path=/test.txt");
      expect(result.method).toBe("DELETE");
      expect(result.path).toBe("/api/v2/files?path=/test.txt");
    });

    it("HEAD request", () => {
      const result = expectHttp("HEAD /api/v2/health");
      expect(result.method).toBe("HEAD");
    });

    it("OPTIONS request", () => {
      const result = expectHttp("OPTIONS /api/v2/files");
      expect(result.method).toBe("OPTIONS");
    });

    it("case-insensitive method", () => {
      const result = expectHttp("get /api/v2/health");
      expect(result.method).toBe("GET");
    });

    it("mixed case method", () => {
      const result = expectHttp("Post /api/v2/files/write");
      expect(result.method).toBe("POST");
    });
  });

  // =========================================================================
  // Local commands (! prefix) → LocalCommand (Decision 4A: allowlist)
  // =========================================================================
  describe("local commands (type: local)", () => {
    it("!init", () => {
      const result = expectLocal("!init");
      expect(result.command).toBe("init");
      expect(result.args).toEqual([]);
    });

    it("!init --preset shared", () => {
      const result = expectLocal("!init --preset shared");
      expect(result.command).toBe("init");
      expect(result.args).toEqual(["--preset", "shared"]);
    });

    it("!build", () => {
      const result = expectLocal("!build");
      expect(result.command).toBe("build");
      expect(result.args).toEqual([]);
    });

    it("!demo init", () => {
      const result = expectLocal("!demo init");
      expect(result.command).toBe("demo");
      expect(result.args).toEqual(["init"]);
    });

    it("!brick mount my-brick", () => {
      const result = expectLocal("!brick mount my-brick");
      expect(result.command).toBe("brick");
      expect(result.args).toEqual(["mount", "my-brick"]);
    });

    it("!agent spawn", () => {
      const result = expectLocal("!agent spawn");
      expect(result.command).toBe("agent");
      expect(result.args).toEqual(["spawn"]);
    });

    it("!up", () => {
      const result = expectLocal("!up");
      expect(result.command).toBe("up");
      expect(result.args).toEqual([]);
    });

    it("handles leading whitespace after !", () => {
      const result = expectLocal("!  init --preset demo");
      expect(result.command).toBe("init");
      expect(result.args).toEqual(["--preset", "demo"]);
    });

    it("handles leading whitespace before !", () => {
      const result = expectLocal("  !init");
      expect(result.command).toBe("init");
    });

    it("rejects commands not in allowlist", () => {
      expect(parseCommand("!rm /important")).toBeNull();
      expect(parseCommand("!migrate --force")).toBeNull();
      expect(parseCommand("!cat /etc/passwd")).toBeNull();
      expect(parseCommand("!status")).toBeNull();
    });
  });
});
