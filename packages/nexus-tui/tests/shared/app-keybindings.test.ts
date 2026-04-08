/**
 * Tests for the selectBranch keyboard-state selector (#3501).
 *
 * Covers all four branches and their priority ordering:
 * resize > pre-connection > overlay > normal
 */

import { describe, it, expect } from "bun:test";
import { selectBranch } from "../../src/shared/app-keybindings.js";

// Shorthand for fully "normal" state
const normal = { terminalTooSmall: false, showPreConnection: false, overlayOpen: false };

// =============================================================================
// Branch selection
// =============================================================================

describe("selectBranch", () => {
  describe("resize branch", () => {
    it("returns 'resize' when terminal is too small", () => {
      expect(selectBranch({ ...normal, terminalTooSmall: true })).toBe("resize");
    });

    it("resize has highest priority — wins over pre-connection", () => {
      expect(selectBranch({ terminalTooSmall: true, showPreConnection: true, overlayOpen: false })).toBe("resize");
    });

    it("resize has highest priority — wins over overlay", () => {
      expect(selectBranch({ terminalTooSmall: true, showPreConnection: false, overlayOpen: true })).toBe("resize");
    });

    it("resize wins when all flags are true", () => {
      expect(selectBranch({ terminalTooSmall: true, showPreConnection: true, overlayOpen: true })).toBe("resize");
    });
  });

  describe("pre-connection branch", () => {
    it("returns 'pre-connection' when not connected and terminal is ok", () => {
      expect(selectBranch({ ...normal, showPreConnection: true })).toBe("pre-connection");
    });

    it("pre-connection wins over overlay", () => {
      expect(selectBranch({ terminalTooSmall: false, showPreConnection: true, overlayOpen: true })).toBe("pre-connection");
    });
  });

  describe("overlay branch", () => {
    it("returns 'overlay' when an overlay is open", () => {
      expect(selectBranch({ ...normal, overlayOpen: true })).toBe("overlay");
    });
  });

  describe("normal branch", () => {
    it("returns 'normal' when all flags are false", () => {
      expect(selectBranch(normal)).toBe("normal");
    });
  });
});
