/**
 * Tests for the centralized terminal dimensions signal (#3501).
 *
 * Covers:
 * - readDimensions: reads process.stdout with safe defaults
 * - Resize listener: signal updates after debounce on 'resize' event
 * - Debounce: rapid events collapse to one update
 * - isTTY=false: no listener registered, no update on resize
 */

import { describe, it, expect, mock, beforeEach, afterEach } from "bun:test";
import { readDimensions, terminalDimensions, _setDimensionsForTesting } from "../../src/shared/terminal-dimensions.js";
import { TERMINAL_GUARD_MIN_COLS, TERMINAL_GUARD_MIN_ROWS, RESIZE_DEBOUNCE_MS } from "../../src/shared/components/side-nav-utils.js";

// =============================================================================
// readDimensions
// =============================================================================

describe("readDimensions", () => {
  let originalColumns: number | undefined;
  let originalRows: number | undefined;

  beforeEach(() => {
    originalColumns = process.stdout.columns;
    originalRows = process.stdout.rows;
  });

  afterEach(() => {
    Object.defineProperty(process.stdout, "columns", { value: originalColumns, configurable: true });
    Object.defineProperty(process.stdout, "rows", { value: originalRows, configurable: true });
  });

  it("reads columns from process.stdout", () => {
    Object.defineProperty(process.stdout, "columns", { value: 120, configurable: true });
    Object.defineProperty(process.stdout, "rows", { value: 40, configurable: true });
    const d = readDimensions();
    expect(d.width).toBe(120);
    expect(d.height).toBe(40);
  });

  it("falls back to TERMINAL_GUARD_MIN_COLS when columns is undefined", () => {
    Object.defineProperty(process.stdout, "columns", { value: undefined, configurable: true });
    Object.defineProperty(process.stdout, "rows", { value: undefined, configurable: true });
    const d = readDimensions();
    expect(d.width).toBe(TERMINAL_GUARD_MIN_COLS);
    expect(d.height).toBe(TERMINAL_GUARD_MIN_ROWS);
  });

  it("returns an object with width and height", () => {
    const d = readDimensions();
    expect(typeof d.width).toBe("number");
    expect(typeof d.height).toBe("number");
  });
});

// =============================================================================
// Resize listener & debounce
// =============================================================================

describe("resize listener", () => {
  it("no resize listener registered in non-TTY environment", () => {
    // In Bun's test runner process.stdout.isTTY is falsy, so the module's
    // isTTY guard should have prevented listener registration at load time.
    if (process.stdout.isTTY) return; // only meaningful in non-TTY

    // No code in this test suite registers resize listeners outside the
    // terminal-dimensions module itself, so the count should be exactly 0.
    expect(process.stdout.listenerCount("resize")).toBe(0);
  });

  it("signal updates when resize event fires (TTY environment only)", async () => {
    // Only meaningful in a real TTY environment; skip in CI/piped contexts.
    if (!process.stdout.isTTY) return;

    const { terminalDimensions } = await import("../../src/shared/terminal-dimensions.js");
    const before = terminalDimensions();
    expect(typeof before.width).toBe("number");
    expect(typeof before.height).toBe("number");

    // Simulate resize
    Object.defineProperty(process.stdout, "columns", { value: before.width + 10, configurable: true });
    process.stdout.emit("resize");

    // Wait longer than the debounce
    await new Promise<void>((resolve) => setTimeout(resolve, RESIZE_DEBOUNCE_MS + 50));

    expect(terminalDimensions().width).toBe(before.width + 10);

    // Restore
    Object.defineProperty(process.stdout, "columns", { value: before.width, configurable: true });
    process.stdout.emit("resize");
    await new Promise<void>((resolve) => setTimeout(resolve, RESIZE_DEBOUNCE_MS + 50));
  });

  it("rapid resize events collapse to one signal update (debounce)", async () => {
    if (!process.stdout.isTTY) return;

    const { terminalDimensions } = await import("../../src/shared/terminal-dimensions.js");
    const initial = terminalDimensions().width;

    // Fire 20 rapid resize events
    for (let i = 0; i < 20; i++) {
      Object.defineProperty(process.stdout, "columns", { value: 80 + i, configurable: true });
      process.stdout.emit("resize");
    }

    // Immediately after all emits: debounce timer has NOT fired yet.
    // Signal must still be at the initial value — this is the key debounce assertion.
    expect(terminalDimensions().width).toBe(initial);

    // Wait for debounce to settle
    await new Promise<void>((resolve) => setTimeout(resolve, RESIZE_DEBOUNCE_MS + 100));

    // Signal must now reflect the LAST emitted value (80 + 19 = 99), not an intermediate one.
    expect(terminalDimensions().width).toBe(99);

    // Restore
    Object.defineProperty(process.stdout, "columns", { value: initial, configurable: true });
    process.stdout.emit("resize");
    await new Promise<void>((resolve) => setTimeout(resolve, RESIZE_DEBOUNCE_MS + 50));
  });
});

// =============================================================================
// _setDimensionsForTesting — signal injection (works in CI / non-TTY)
// =============================================================================

describe("_setDimensionsForTesting", () => {
  let savedDimensions: { width: number; height: number };

  beforeEach(() => {
    savedDimensions = { ...terminalDimensions() };
  });

  afterEach(() => {
    // Restore signal to whatever it was before the test
    _setDimensionsForTesting(savedDimensions);
  });

  it("directly updates the signal without needing process.stdout or isTTY", () => {
    _setDimensionsForTesting({ width: 59, height: 20 });
    expect(terminalDimensions().width).toBe(59);
    expect(terminalDimensions().height).toBe(20);
  });

  it("reflects the exact boundary: 59 cols is below minimum", () => {
    _setDimensionsForTesting({ width: 59, height: 30 });
    expect(terminalDimensions().width).toBeLessThan(TERMINAL_GUARD_MIN_COLS);
  });

  it("reflects the exact boundary: 60 cols is at minimum", () => {
    _setDimensionsForTesting({ width: 60, height: 30 });
    expect(terminalDimensions().width).toBe(TERMINAL_GUARD_MIN_COLS);
  });

  it("reflects the exact row boundary: 23 rows is below minimum", () => {
    _setDimensionsForTesting({ width: 80, height: 23 });
    expect(terminalDimensions().height).toBeLessThan(TERMINAL_GUARD_MIN_ROWS);
  });

  it("reflects the exact row boundary: 24 rows is at minimum", () => {
    _setDimensionsForTesting({ width: 80, height: 24 });
    expect(terminalDimensions().height).toBe(TERMINAL_GUARD_MIN_ROWS);
  });
});

// =============================================================================
// Constants
// =============================================================================

describe("constants used by terminal-dimensions", () => {
  it("TERMINAL_GUARD_MIN_COLS is 60", () => {
    expect(TERMINAL_GUARD_MIN_COLS).toBe(60);
  });

  it("TERMINAL_GUARD_MIN_ROWS is 24", () => {
    expect(TERMINAL_GUARD_MIN_ROWS).toBe(24);
  });

  it("RESIZE_DEBOUNCE_MS is 150", () => {
    expect(RESIZE_DEBOUNCE_MS).toBe(150);
  });
});
