import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import type { FetchClient } from "@nexus-ai-fs/api-client";
import { App } from "../../src/app.js";
import { useGlobalStore } from "../../src/stores/global-store.js";
import { useSearchStore } from "../../src/stores/search-store.js";
import { createWorkerManager } from "../../src/worker/worker-manager.js";
import { createNativeFixture } from "../helpers/native-fixture.js";
import { testRender } from "../helpers/render.js";

let fixture: ReturnType<typeof createNativeFixture>;
let worker: ReturnType<typeof createWorkerManager>;
let setup: Awaited<ReturnType<typeof testRender>>;
async function frameContaining(text: string): Promise<string> {
  const deadline = performance.now() + 3000;
  do {
    await setup.renderOnce();
    const frame = setup.captureCharFrame();
    if (frame.includes(text)) return frame;
    await Bun.sleep(10);
  } while (performance.now() < deadline);
  throw new Error(`Missing ${text}:\n${setup.captureCharFrame()}`);
}
beforeEach(async () => {
  fixture = createNativeFixture();
  const config = { baseUrl: fixture.url, apiKey: "tui-fixture", zoneId: "sharedzone", maxRetries: 0, transformKeys: false };
  worker = createWorkerManager(config);
  useGlobalStore.setState({ config, client: worker.client as unknown as FetchClient, connectionStatus: "connected", userInfo: fixture.principal,
    activePanel: "files", connectionError: null, serverVersion: "fixture-1" });
  useSearchStore.getState().reset();
  setup = await testRender(() => <App />, { width: 180, height: 35 });
  await frameContaining("/notes.txt");
});
afterEach(() => { setup?.renderer.destroy(); worker?.terminate(); fixture?.stop(); });

describe("native terminal interaction through the worker", () => {
  it("opens real bytes, edits and saves through the native receipt", async () => {
    setup.mockInput.pressKey("e");
    await frameContaining("Ctrl+S:save");
    setup.mockInput.pressKey("END");
    await setup.mockInput.typeText(" changed");
    await frameContaining("[modified]");
    setup.mockInput.pressKey("s", { ctrl: true });
    const deadline = performance.now() + 3000;
    while (!new TextDecoder().decode(fixture.files.get("/notes.txt")).includes("changed") && performance.now() < deadline) await Bun.sleep(10);
    expect(new TextDecoder().decode(fixture.files.get("/notes.txt"))).toBe("hello changed");
    await setup.renderOnce();
    expect(setup.captureCharFrame()).not.toContain("[modified]");
    setup.mockInput.pressEscape();
    await frameContaining("Files: /");
    expect(fixture.requests.every((request) => request.path.startsWith("/v2/"))).toBe(true);
  });

  it("invalid save acknowledgement keeps the draft and cancelling discard preserves it", async () => {
    fixture.control.invalidWrite = true;
    setup.mockInput.pressKey("e");
    await frameContaining("Ctrl+S:save");
    setup.mockInput.pressKey("END");
    await setup.mockInput.typeText(" unsaved");
    setup.mockInput.pressKey("s", { ctrl: true });
    await frameContaining("Save failed: Invalid native file response");
    expect(new TextDecoder().decode(fixture.files.get("/notes.txt"))).toBe("hello");
    setup.mockInput.pressEscape();
    await frameContaining("Discard changes");
    setup.mockInput.pressKey("n");
    const frame = await frameContaining("hello unsaved");
    expect(frame).toContain("[modified]");
    expect(frame).not.toContain("Discard changes");
  });

  it("native prompt submits the actual Unicode directory path", async () => {
    setup.mockInput.pressKey("m");
    await frameContaining("Create directory");
    setup.mockInput.pressKey("END");
    await setup.mockInput.pasteBracketedText("日本語 & folder");
    setup.mockInput.pressEnter();
    await frameContaining("/日本語 & folder");
    expect(fixture.directories.has("/日本語 & folder")).toBe(true);
  });

  it("late save acknowledgement preserves edits made during transfer", async () => {
    let release!: () => void;
    fixture.control.writeGate = new Promise<void>((resolve) => { release = resolve; });
    setup.mockInput.pressKey("e");
    await frameContaining("Ctrl+S:save");
    setup.mockInput.pressKey("END");
    await setup.mockInput.typeText(" first");
    setup.mockInput.pressKey("s", { ctrl: true });
    await frameContaining("Saving…");
    await setup.mockInput.typeText(" later");
    release();
    const deadline = performance.now() + 3000;
    while (!new TextDecoder().decode(fixture.files.get("/notes.txt")).includes("first") && performance.now() < deadline) await Bun.sleep(10);
    expect(new TextDecoder().decode(fixture.files.get("/notes.txt"))).toBe("hello first");
    const frame = await frameContaining("hello first later");
    expect(frame).toContain("[modified]");
  });

  it("delete confirmation cancels or removes the selected native path", async () => {
    setup.mockInput.pressKey("d");
    await frameContaining("Delete native path");
    setup.mockInput.pressKey("n");
    await frameContaining("Files: /");
    expect(fixture.files.has("/notes.txt")).toBe(true);
    setup.mockInput.pressKey("d");
    await frameContaining("Delete native path");
    setup.mockInput.pressKey("y");
    const deadline = performance.now() + 3000;
    while (fixture.files.has("/notes.txt") && performance.now() < deadline) await Bun.sleep(10);
    expect(fixture.files.has("/notes.txt")).toBe(false);
    await setup.renderOnce();
    expect(setup.captureCharFrame()).not.toContain("/notes.txt");
  });

  it("Search keys execute the native query and render authorization errors", async () => {
    setup.mockInput.pressKey("2");
    await frameContaining("Search zone:sharedzone");
    setup.mockInput.pressKey("/");
    await frameContaining("Query (Enter");
    await setup.mockInput.typeText("needle");
    setup.mockInput.pressEnter();
    await frameContaining("Results: 1 found");
    expect(setup.captureCharFrame()).toContain("needle");
    setup.mockInput.pressKey("/");
    await frameContaining("Query (Enter");
    for (let index = 0; index < "needle".length; index++) setup.mockInput.pressBackspace();
    await setup.mockInput.typeText("denied");
    setup.mockInput.pressEnter();
    await frameContaining("Error: search denied");
    expect(setup.captureCharFrame()).toContain("Search failed");
  });

  it("Console sends native HTTP through the same original credential", async () => {
    setup.mockInput.pressKey("3");
    await frameContaining("Native Console");
    setup.mockInput.pressKey("i");
    await frameContaining("Native HTTP request");
    setup.mockInput.pressEnter();
    await frameContaining("200 OK");
    expect(setup.captureCharFrame()).toContain("fixture-user");
    expect(fixture.requests.at(-1)).toEqual({ method: "GET", path: "/v2/auth/whoami", authorization: "Bearer tui-fixture" });
  });

  it("large files stay in a bounded preview and cannot be partially edited", async () => {
    fixture.files.set("/notes.txt", new Uint8Array(2 * 1024 * 1024).fill(65));
    setup.mockInput.pressKey("e");
    const frame = await frameContaining("Text editing is limited to 1 MiB.");
    expect(frame).not.toContain("Ctrl+S:save");
    expect(fixture.requests.some((request) => request.method === "PUT")).toBe(false);
  });
});
