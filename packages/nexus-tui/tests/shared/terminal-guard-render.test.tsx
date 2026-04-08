/**
 * Render tests for the TerminalGuard component (#3501).
 *
 * Uses OpenTUI's testRender to verify actual terminal output.
 *
 * Covers:
 * - Children rendered when terminal is above minimum (60×24)
 * - Resize message shown when terminal is below minimum
 * - Exact boundary at 59/60 cols and 23/24 rows
 * - Non-TTY: children always rendered, no guard
 */

import { describe, it, expect, afterEach, beforeEach } from "bun:test";
import { testRender } from "../helpers/render.js";
import { TerminalGuard } from "../../src/shared/components/terminal-guard.js";
import { TERMINAL_GUARD_MIN_COLS, TERMINAL_GUARD_MIN_ROWS } from "../../src/shared/components/side-nav-utils.js";
import { terminalDimensions, _setDimensionsForTesting } from "../../src/shared/terminal-dimensions.js";

// =============================================================================
// Helpers
// =============================================================================

type TestSetup = Awaited<ReturnType<typeof testRender>>;

let setup: TestSetup;

async function renderGuard(
  options: { width: number; height: number },
): Promise<string> {
  setup = await testRender(
    () => (
      <TerminalGuard>
        <text>{"app content"}</text>
      </TerminalGuard>
    ),
    { width: options.width, height: options.height },
  );
  await setup.renderOnce();
  return setup.captureCharFrame();
}

afterEach(() => {
  if (setup) {
    setup.renderer.destroy();
  }
});

// =============================================================================
// Children rendered when above minimum
// =============================================================================

describe("above minimum size", () => {
  it("renders children at exactly the minimum (60×24)", async () => {
    // TerminalGuard is non-TTY in test runner, so children always pass through.
    // This test verifies the non-TTY path renders children unconditionally.
    const frame = await renderGuard({ width: TERMINAL_GUARD_MIN_COLS, height: TERMINAL_GUARD_MIN_ROWS });
    expect(frame).toContain("app content");
  });

  it("renders children well above minimum (120×40)", async () => {
    const frame = await renderGuard({ width: 120, height: 40 });
    expect(frame).toContain("app content");
  });

  it("renders children at 80×30 (collapsed sidebar range)", async () => {
    const frame = await renderGuard({ width: 80, height: 30 });
    expect(frame).toContain("app content");
  });
});

// =============================================================================
// Boundary at exactly 59 vs 60 columns
// =============================================================================

describe("column boundary", () => {
  it("renders children at cols=60 (at minimum — no guard)", async () => {
    const frame = await renderGuard({ width: 60, height: 30 });
    expect(frame).toContain("app content");
  });

  it("shows resize message at cols=59 (one below minimum) — TTY only", async () => {
    // In non-TTY test environments the guard is bypassed entirely.
    // This test documents the expected TTY behavior.
    if (!process.stdout.isTTY) {
      const frame = await renderGuard({ width: 59, height: 30 });
      expect(frame).toContain("app content");
      return;
    }
    const frame = await renderGuard({ width: 59, height: 30 });
    expect(frame).toContain("Resize to at least");
    expect(frame).not.toContain("app content");
  });

  it("renders children at cols=0 in non-TTY (guard bypassed)", async () => {
    if (process.stdout.isTTY) return; // only test non-TTY path here
    const frame = await renderGuard({ width: 0, height: 0 });
    expect(frame).toContain("app content");
  });
});

// =============================================================================
// Boundary at exactly 23 vs 24 rows
// =============================================================================

describe("row boundary", () => {
  it("renders children at rows=24 (at minimum — no guard)", async () => {
    const frame = await renderGuard({ width: 80, height: 24 });
    expect(frame).toContain("app content");
  });

  it("shows resize message at rows=23 (one below minimum) — TTY only", async () => {
    if (!process.stdout.isTTY) {
      const frame = await renderGuard({ width: 80, height: 23 });
      expect(frame).toContain("app content");
      return;
    }
    const frame = await renderGuard({ width: 80, height: 23 });
    expect(frame).toContain("Resize to at least");
    expect(frame).not.toContain("app content");
  });
});

// =============================================================================
// Resize message content (TTY only)
// =============================================================================

describe("resize message", () => {
  it("resize message includes minimum dimensions", async () => {
    if (!process.stdout.isTTY) return;
    const frame = await renderGuard({ width: 40, height: 10 });
    expect(frame).toContain(`${TERMINAL_GUARD_MIN_COLS}×${TERMINAL_GUARD_MIN_ROWS}`);
  });

  it("resize message shows current dimensions", async () => {
    if (!process.stdout.isTTY) return;
    const frame = await renderGuard({ width: 40, height: 10 });
    expect(frame).toContain("current:");
  });
});

// =============================================================================
// Signal-driven boundary tests (deterministic, CI-runnable)
// =============================================================================

describe("signal-driven boundary (via _setDimensionsForTesting)", () => {
  let savedDimensions: { width: number; height: number };
  let savedIsTTY: boolean | undefined;

  beforeEach(() => {
    savedDimensions = { ...terminalDimensions() };
    savedIsTTY = process.stdout.isTTY;
  });

  afterEach(() => {
    _setDimensionsForTesting(savedDimensions);
    Object.defineProperty(process.stdout, "isTTY", { value: savedIsTTY, configurable: true });
    // Destroy here; null setup so the outer afterEach doesn't double-destroy.
    if (setup) {
      setup.renderer.destroy();
      setup = undefined as unknown as TestSetup;
    }
  });

  it("shows resize message when signal reports below-minimum cols (isTTY=true)", async () => {
    Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });
    _setDimensionsForTesting({ width: 59, height: 30 });

    setup = await testRender(
      () => (
        <TerminalGuard>
          <text>{"app content"}</text>
        </TerminalGuard>
      ),
      { width: 80, height: 30 },
    );
    await setup.renderOnce();
    const frame = setup.captureCharFrame();
    expect(frame).toContain("Terminal too small");
    expect(frame).not.toContain("app content");
  });

  it("renders children when signal reports at-minimum cols (isTTY=true)", async () => {
    Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });
    _setDimensionsForTesting({ width: 60, height: 24 });

    setup = await testRender(
      () => (
        <TerminalGuard>
          <text>{"app content"}</text>
        </TerminalGuard>
      ),
      { width: 80, height: 30 },
    );
    await setup.renderOnce();
    const frame = setup.captureCharFrame();
    expect(frame).toContain("app content");
  });

  it("shows resize message when signal reports below-minimum rows (isTTY=true)", async () => {
    Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });
    _setDimensionsForTesting({ width: 80, height: 23 });

    setup = await testRender(
      () => (
        <TerminalGuard>
          <text>{"app content"}</text>
        </TerminalGuard>
      ),
      { width: 80, height: 30 },
    );
    await setup.renderOnce();
    const frame = setup.captureCharFrame();
    expect(frame).toContain("Terminal too small");
    expect(frame).not.toContain("app content");
  });

  it("resize message includes minimum dimensions", async () => {
    Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });
    _setDimensionsForTesting({ width: 40, height: 10 });

    setup = await testRender(
      () => (
        <TerminalGuard>
          <text>{"app content"}</text>
        </TerminalGuard>
      ),
      { width: 80, height: 30 },
    );
    await setup.renderOnce();
    const frame = setup.captureCharFrame();
    expect(frame).toContain(`${TERMINAL_GUARD_MIN_COLS}`);
    expect(frame).toContain(`${TERMINAL_GUARD_MIN_ROWS}`);
  });
});

// =============================================================================
// Non-TTY path
// =============================================================================

describe("non-TTY path", () => {
  it("renders children unconditionally in non-TTY environment", async () => {
    if (process.stdout.isTTY) {
      // In TTY environments this test is a no-op
      return;
    }
    // The guard is bypassed; even below-minimum sizes show children.
    // Use a frame wide enough to render the text content.
    const frame = await renderGuard({ width: 20, height: 5 });
    expect(frame).toContain("app content");
  });
});
