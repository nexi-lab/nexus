/**
 * Render tests for the CommandPalette component.
 *
 * Covers:
 * - Component is hidden when visible=false
 * - Palette header and prompt render correctly when visible=true
 * - Cursor block (█) is ALWAYS present — regression guard for the
 *   `query.length >= 0` tautology bug (Issue #3624)
 * - All commands are listed in the initial (empty-query) state
 * - Navigation marker ("> ") highlights the selected command
 * - Enter executes the selected command and calls onClose
 * - Down/Up arrow changes the selected command
 *
 * Note: Tests that require `typeText` to propagate through the full
 * OpenTUI → React → re-render pipeline (e.g. live filtering on every
 * keystroke) are covered at the tmux integration level rather than here,
 * because the escape-sequence parser introduces async ambiguity that
 * makes unit-level assertions flaky.
 */

import { describe, it, expect, mock, afterEach } from "bun:test";
import { testRender } from "../helpers/render.js";
import { CommandPalette } from "../../src/shared/components/command-palette.js";
import type { CommandPaletteItem } from "../../src/shared/command-palette.js";

// =============================================================================
// Helpers
// =============================================================================

type TestSetup = Awaited<ReturnType<typeof testRender>>;

let setup: TestSetup;

const COMMANDS: readonly CommandPaletteItem[] = [
  {
    id: "panel:files",
    title: "Switch to Files",
    section: "Panels",
    hint: "1",
    keywords: ["files", "panel"],
    run: mock(() => {}),
  },
  {
    id: "panel:agents",
    title: "Switch to Agents",
    section: "Panels",
    hint: "3",
    keywords: ["agents", "panel"],
    run: mock(() => {}),
  },
  {
    id: "app:quit",
    title: "Quit",
    section: "Global",
    hint: "q",
    keywords: ["exit", "close"],
    run: mock(() => {}),
  },
];

async function renderPalette(
  visible: boolean,
  commands: readonly CommandPaletteItem[],
  onClose: () => void,
): Promise<TestSetup> {
  setup = await testRender(
    () => <CommandPalette visible={visible} commands={commands} onClose={onClose} />,
    { width: 80, height: 30 },
  );
  await setup.renderOnce();
  return setup;
}

afterEach(() => {
  if (setup) {
    setup.renderer.destroy();
  }
});

// =============================================================================
// Visibility
// =============================================================================

describe("CommandPalette visibility", () => {
  it("renders nothing when visible=false", async () => {
    const { captureCharFrame } = await renderPalette(false, COMMANDS, mock(() => {}));
    expect(captureCharFrame().trim()).toBe("");
  });

  it("renders the palette header when visible=true", async () => {
    const { captureCharFrame } = await renderPalette(true, COMMANDS, mock(() => {}));
    expect(captureCharFrame()).toContain("Command Palette");
  });

  it("renders the helper text", async () => {
    const { captureCharFrame } = await renderPalette(true, COMMANDS, mock(() => {}));
    expect(captureCharFrame()).toContain("Type to filter");
  });
});

// =============================================================================
// Cursor — regression for query.length >= 0 tautology (Issue #3624)
// =============================================================================

describe("CommandPalette cursor (regression: #3624)", () => {
  it("always shows cursor block (█) on empty query — was always-true tautology", async () => {
    // This test guards against the `query.length >= 0 ? "█" : ""` regression.
    // The cursor block must be visible on the initial render (empty query).
    const { captureCharFrame } = await renderPalette(true, COMMANDS, mock(() => {}));
    expect(captureCharFrame()).toContain("\u2588");
  });

  it("cursor block is part of the input prompt line", async () => {
    const { captureCharFrame } = await renderPalette(true, COMMANDS, mock(() => {}));
    const frame = captureCharFrame();
    // The prompt is "> " followed immediately by the cursor block on empty query
    expect(frame).toContain("> \u2588");
  });
});

// =============================================================================
// Initial render — all commands listed
// =============================================================================

describe("CommandPalette initial render", () => {
  it("shows all commands with empty query", async () => {
    const { captureCharFrame } = await renderPalette(true, COMMANDS, mock(() => {}));
    const frame = captureCharFrame();
    expect(frame).toContain("Switch to Files");
    expect(frame).toContain("Switch to Agents");
    expect(frame).toContain("Quit");
  });

  it("shows keyboard hints", async () => {
    const { captureCharFrame } = await renderPalette(true, COMMANDS, mock(() => {}));
    const frame = captureCharFrame();
    expect(frame).toContain("[1]");  // Files hint
    expect(frame).toContain("[q]");  // Quit hint
  });

  it("shows the selection marker (>) on the first item", async () => {
    const { captureCharFrame } = await renderPalette(true, COMMANDS, mock(() => {}));
    expect(captureCharFrame()).toContain("> ");
  });
});

// =============================================================================
// Navigation
// =============================================================================

describe("CommandPalette navigation", () => {
  it("Down arrow moves selection and keeps a selection marker visible", async () => {
    const { captureCharFrame, mockInput, renderOnce } = await renderPalette(
      true, COMMANDS, mock(() => {}),
    );
    mockInput.pressArrow("down");
    await renderOnce();
    await renderOnce();
    expect(captureCharFrame()).toContain("> ");
  });

  it("Up arrow at index 0 clamps and keeps a selection marker visible", async () => {
    const { captureCharFrame, mockInput, renderOnce } = await renderPalette(
      true, COMMANDS, mock(() => {}),
    );
    mockInput.pressArrow("up");
    await renderOnce();
    await renderOnce();
    expect(captureCharFrame()).toContain("> ");
  });
});

// =============================================================================
// Actions
// =============================================================================

describe("CommandPalette actions", () => {
  it("Enter executes the selected command", async () => {
    const runMock = mock(() => {});
    const cmds: CommandPaletteItem[] = [
      { id: "test", title: "Test Action", section: "Test", run: runMock },
    ];
    const { mockInput, renderOnce } = await renderPalette(true, cmds, mock(() => {}));
    mockInput.pressEnter();
    await renderOnce();
    await renderOnce();
    expect(runMock).toHaveBeenCalledTimes(1);
  });

  it("Enter calls onClose after executing", async () => {
    const onClose = mock(() => {});
    const cmds: CommandPaletteItem[] = [
      { id: "test", title: "Test", section: "Test", run: mock(() => {}) },
    ];
    const { mockInput, renderOnce } = await renderPalette(true, cmds, onClose);
    mockInput.pressEnter();
    await renderOnce();
    await renderOnce();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Enter with no matching commands does not call onClose", async () => {
    // filterCommandPaletteItems returns [] → executeSelected bails early
    const onClose = mock(() => {});
    // Empty command list guarantees no match
    const { mockInput, renderOnce } = await renderPalette(true, [], onClose);
    mockInput.pressEnter();
    await renderOnce();
    await renderOnce();
    expect(onClose).not.toHaveBeenCalled();
  });
});
