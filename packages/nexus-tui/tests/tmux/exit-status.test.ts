import { describe, expect, test } from "bun:test";
import { TmuxSession } from "./tmux-harness.js";

describe.skipIf(process.platform === "win32")("Terminal exit status", () => {
  test("preserves a real nonzero child exit code", async () => {
    const session = await TmuxSession.create({
      command: "printf 'READY\\n'; read reply; exit 23",
    });
    try {
      await session.waitForText("READY");
      await session.sendKeys("Enter");
      expect(await session.waitForExit(5000)).toBe(23);
    } finally {
      await session.destroy();
    }
  });
});
