import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { createNativeFixture } from "../helpers/native-fixture.js";
import { TmuxSession } from "./tmux-harness.js";

describe.skipIf(process.platform === "win32")("native panels in the actual terminal process", () => {
  let fixture: ReturnType<typeof createNativeFixture>;
  let session: TmuxSession;
  beforeEach(async () => {
    fixture = createNativeFixture();
    session = await TmuxSession.create({ width: 140, height: 40, url: fixture.url, apiKey: "tui-fixture", zoneId: "sharedzone" });
    await session.waitForText("user:fixture-user", 10000);
    await session.waitForText("/notes.txt", 5000);
  });
  afterEach(async () => { await session?.destroy(); fixture?.stop(); });

  test("edits, survives terminal resizing and saves exact native bytes", async () => {
    await session.sendKeys("e");
    await session.waitForText("Ctrl+S:save");
    await session.sendKeys("End");
    await session.sendKeys(" changed");
    await session.waitForText("hello changed");
    await session.resize(50, 20);
    await session.waitForText("Terminal too small");
    await session.resize(140, 40);
    await session.waitForText("hello changed");
    await session.sendKeys("C-s");
    await session.waitForPattern(/\/notes\.txt \| Ctrl\+S:save/);
    expect(new TextDecoder().decode(fixture.files.get("/notes.txt"))).toBe("hello changed");
    await session.sendKeys("Escape");
    await session.waitForText("Files: /");
    expect(fixture.requests.every((request) => request.path.startsWith("/v2/"))).toBe(true);
  }, 25000);

  test("Search and Console use real keystrokes and the original native credential", async () => {
    await session.sendKeys("2");
    await session.waitForText("Search zone:sharedzone");
    await session.sendKeys("/");
    await session.waitForText("Query (Enter");
    await session.sendKeys("needle");
    await session.sendKeys("Enter");
    const search = await session.waitForText("Results: 1 found");
    expect(search).toContain("needle");
    await session.sendKeys("3");
    await session.waitForText("Native Console");
    await session.sendKeys("i");
    await session.waitForText("Native HTTP request");
    await session.sendKeys("Enter");
    await session.waitForText("200 OK");
    expect(fixture.requests.at(-1)).toEqual({ method: "GET", path: "/v2/auth/whoami", authorization: "Bearer tui-fixture" });
    await session.sendKeys("q");
    expect(await session.waitForExit(5000)).toBe(0);
  }, 25000);
});
