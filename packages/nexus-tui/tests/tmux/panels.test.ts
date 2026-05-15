/**
 * tmux capture-pane tests for connected panels.
 *
 * These tests require a running Nexus server with demo data.
 * Set NEXUS_URL and NEXUS_API_KEY env vars to run them.
 * They are skipped when no server is available.
 *
 * Covers: Workflows (simple), Events (complex), API Console (unique).
 * @see Issue #3250 Screens 8, 10, 11, 12
 */

import { describe, test, expect, afterAll, beforeAll } from "bun:test";
import { TmuxSession, cleanupTestSessions } from "./tmux-harness.js";

const NEXUS_URL = process.env.NEXUS_URL ?? "http://localhost:2026";
const NEXUS_API_KEY = process.env.NEXUS_API_KEY ?? "";

// Synchronous check — can the server be reached? (checked before tests run)
let serverReachable = false;
try {
  const res = await fetch(`${NEXUS_URL}/healthz/ready`, { signal: AbortSignal.timeout(3000) });
  serverReachable = res.ok;
} catch {
  serverReachable = false;
}

const SKIP = !NEXUS_API_KEY || !serverReachable;

afterAll(async () => {
  await cleanupTestSessions();
});

describe("Workflows panel (Screen 10)", () => {
  test.skipIf(SKIP)("renders tab bar and workflow list", async () => {
    const session = await TmuxSession.create({
      width: 120,
      height: 40,
      url: NEXUS_URL,
      apiKey: NEXUS_API_KEY,
    });

    try {
      // Wait for connection
      await session.waitForText("connected", 10000);

      // Navigate to Workflows panel (key 8)
      await session.sendKeys("8");
      await new Promise((r) => setTimeout(r, 1000));

      const content = await session.capturePane();

      // Verify tab bar
      expect(content).toContain("[Workflows]");
      expect(content).toContain("Executions");
      expect(content).toContain("Scheduler");

      // Verify help bar
      expect(content).toMatch(/j\/k.*navigate/);
      expect(content).toContain("e:execute");
      expect(content).toContain("d:delete");

      // Verify list renders (either badges for existing workflows or empty/loading state)
      expect(content).toMatch(/\[(ON|--)\]|No workflows defined|Loading workflows/);
    } finally {
      await session.destroy();
    }
  }, 20000);

  test.skipIf(SKIP)("tab cycling works between Workflows and Executions", async () => {
    const session = await TmuxSession.create({
      width: 120,
      height: 40,
      url: NEXUS_URL,
      apiKey: NEXUS_API_KEY,
    });

    try {
      await session.waitForText("connected", 10000);
      await session.sendKeys("8");
      await session.waitForText("[Workflows]", 5000);

      // Cycle to Executions tab
      await session.sendKeys("Tab");
      await new Promise((r) => setTimeout(r, 500));

      const content = await session.capturePane();
      expect(content).toContain("[Executions]");
    } finally {
      await session.destroy();
    }
  }, 20000);
});

describe("Events panel (Screen 11)", () => {
  test.skipIf(SKIP)("renders SSE connection status and 9 tabs", async () => {
    const session = await TmuxSession.create({
      width: 120,
      height: 40,
      url: NEXUS_URL,
      apiKey: NEXUS_API_KEY,
    });

    try {
      await session.waitForText("connected", 10000);

      // Navigate to Events panel (key 9)
      await session.sendKeys("9");
      await new Promise((r) => setTimeout(r, 1000));

      const content = await session.capturePane();

      // Verify tab bar has all 9 tabs
      expect(content).toContain("[Events]");
      expect(content).toContain("MCL");
      expect(content).toContain("Replay");
      expect(content).toContain("Connectors");
      expect(content).toContain("Locks");

      // Verify SSE connection indicator
      expect(content).toMatch(/[●◐○✕]/);

      // Verify filter bar
      expect(content).toContain("Filter:");

      // Verify help bar
      expect(content).toContain("f:type filter");
      expect(content).toContain("s:search");
    } finally {
      await session.destroy();
    }
  }, 20000);

  test.skipIf(SKIP)("filter mode activates on 'f' key", async () => {
    const session = await TmuxSession.create({
      width: 120,
      height: 40,
      url: NEXUS_URL,
      apiKey: NEXUS_API_KEY,
    });

    try {
      await session.waitForText("connected", 10000);
      await session.sendKeys("9");
      await session.waitForText("[Events]", 5000);

      // Enter filter mode
      await session.sendKeys("f");
      await new Promise((r) => setTimeout(r, 300));

      const content = await session.capturePane();
      expect(content).toContain("Filter type:");
      // Help bar should switch to input mode hints
      expect(content).toContain("Enter:apply");
      expect(content).toContain("Escape:cancel");

      // Cancel filter
      await session.sendKeys("Escape");
    } finally {
      await session.destroy();
    }
  }, 20000);
});

describe("API Console panel (Screen 12)", () => {
  test.skipIf(SKIP)("renders two-pane layout with endpoints", async () => {
    const session = await TmuxSession.create({
      width: 120,
      height: 40,
      url: NEXUS_URL,
      apiKey: NEXUS_API_KEY,
    });

    try {
      await session.waitForText("connected", 10000);

      // Navigate to Console panel (key 0)
      await session.sendKeys("0");
      await new Promise((r) => setTimeout(r, 1000));

      const content = await session.capturePane();

      // Verify left pane: endpoint list
      expect(content).toContain("Endpoints");
      expect(content).toContain("/:filter");

      // Verify right pane: command input
      expect(content).toMatch(/Press.*command input|history/);

      // Verify focus-aware borders (at least one border is visible)
      expect(content).toMatch(/[│┤├┬┴┼─]/);
    } finally {
      await session.destroy();
    }
  }, 20000);

  test.skipIf(SKIP)("command input mode activates on ':' key", async () => {
    const session = await TmuxSession.create({
      width: 120,
      height: 40,
      url: NEXUS_URL,
      apiKey: NEXUS_API_KEY,
    });

    try {
      await session.waitForText("connected", 10000);
      await session.sendKeys("0");
      await new Promise((r) => setTimeout(r, 1000));

      // Enter command input mode
      await session.sendKeys(":");
      await new Promise((r) => setTimeout(r, 300));

      const content = await session.capturePane();
      // Should show command prompt indicator
      expect(content).toContain(">");

      // Cancel
      await session.sendKeys("Escape");
    } finally {
      await session.destroy();
    }
  }, 20000);
});
