import { afterAll, describe, expect, test } from "bun:test";
import { TmuxSession, cleanupTestSessions } from "./tmux-harness.js";
afterAll(cleanupTestSessions);
describe.skipIf(process.platform === "win32")("native CLI connection and exit", () => {
  test("shows explicit native connection guidance and preserves a zero quit status", async () => {
    const session = await TmuxSession.create({ width: 120, height: 40, url: "http://127.0.0.1:1", apiKey: "unreachable-fixture" });
    try {
      const frame = await session.waitForText("Nexus native", 8000);
      expect(frame).toContain("1:Files");
      expect(frame).toContain("3:Console");
      expect(frame).not.toContain("nexus serve");
      await session.sendKeys("q");
      expect(await session.waitForExit(5000)).toBe(0);
    } finally { await session.destroy(); }
  }, 15000);
});
