import { describe, expect, it } from "bun:test";
import {
  filterCommandPaletteItems,
  type CommandPaletteItem,
} from "../../src/shared/command-palette.js";

const ITEMS: readonly CommandPaletteItem[] = [
  {
    id: "panel:files",
    title: "Switch to Files",
    section: "Panels",
    hint: "1",
    keywords: ["panel", "files"],
    run: () => {},
  },
  {
    id: "global:help",
    title: "Open help overlay",
    section: "Global",
    hint: "?",
    keywords: ["shortcuts", "bindings"],
    run: () => {},
  },
  {
    id: "global:quit",
    title: "Quit Nexus TUI",
    section: "Global",
    hint: "q",
    keywords: ["exit"],
    run: () => {},
  },
];

describe("filterCommandPaletteItems", () => {
  it("returns all items for empty query", () => {
    expect(filterCommandPaletteItems(ITEMS, "")).toHaveLength(3);
  });

  it("matches title and keywords case-insensitively", () => {
    expect(filterCommandPaletteItems(ITEMS, "files")[0]?.id).toBe("panel:files");
    expect(filterCommandPaletteItems(ITEMS, "bindings")[0]?.id).toBe("global:help");
  });

  it("prefers title prefix matches", () => {
    const results = filterCommandPaletteItems(ITEMS, "q");
    expect(results[0]?.id).toBe("global:quit");
  });
});
