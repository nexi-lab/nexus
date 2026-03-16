/**
 * Tests for command history navigation and input mode — Phase 0, Issue #9A.
 */

import { describe, it, expect, beforeEach } from "bun:test";
import { useApiConsoleStore } from "../../src/stores/api-console-store.js";

describe("ApiConsoleStore — command history", () => {
  beforeEach(() => {
    useApiConsoleStore.setState({
      commandHistory: [],
      historyIndex: -1,
      commandInputMode: false,
      commandInputBuffer: "",
    });
  });

  describe("setCommandInputMode", () => {
    it("enables command input mode and clears buffer", () => {
      useApiConsoleStore.getState().setCommandInputBuffer("leftover");
      useApiConsoleStore.getState().setCommandInputMode(true);
      const state = useApiConsoleStore.getState();
      expect(state.commandInputMode).toBe(true);
      expect(state.commandInputBuffer).toBe("");
      expect(state.historyIndex).toBe(-1);
    });

    it("disables command input mode", () => {
      useApiConsoleStore.getState().setCommandInputMode(true);
      useApiConsoleStore.getState().setCommandInputMode(false);
      expect(useApiConsoleStore.getState().commandInputMode).toBe(false);
    });
  });

  describe("setCommandInputBuffer", () => {
    it("sets the buffer", () => {
      useApiConsoleStore.getState().setCommandInputBuffer("GET /api");
      expect(useApiConsoleStore.getState().commandInputBuffer).toBe("GET /api");
    });
  });

  describe("navigateHistory", () => {
    it("does nothing with empty history", () => {
      useApiConsoleStore.getState().navigateHistory("up");
      expect(useApiConsoleStore.getState().historyIndex).toBe(-1);
      expect(useApiConsoleStore.getState().commandInputBuffer).toBe("");
    });

    it("navigates up to most recent entry", () => {
      useApiConsoleStore.setState({
        commandHistory: ["GET /health", "POST /files"],
      });
      useApiConsoleStore.getState().navigateHistory("up");
      const state = useApiConsoleStore.getState();
      expect(state.historyIndex).toBe(1); // last entry
      expect(state.commandInputBuffer).toBe("POST /files");
    });

    it("navigates up through full history", () => {
      useApiConsoleStore.setState({
        commandHistory: ["first", "second", "third"],
      });
      useApiConsoleStore.getState().navigateHistory("up");
      expect(useApiConsoleStore.getState().commandInputBuffer).toBe("third");

      useApiConsoleStore.getState().navigateHistory("up");
      expect(useApiConsoleStore.getState().commandInputBuffer).toBe("second");

      useApiConsoleStore.getState().navigateHistory("up");
      expect(useApiConsoleStore.getState().commandInputBuffer).toBe("first");
    });

    it("stops at oldest entry", () => {
      useApiConsoleStore.setState({
        commandHistory: ["first", "second"],
      });
      useApiConsoleStore.getState().navigateHistory("up");
      useApiConsoleStore.getState().navigateHistory("up");
      useApiConsoleStore.getState().navigateHistory("up"); // should stay at 0
      expect(useApiConsoleStore.getState().historyIndex).toBe(0);
      expect(useApiConsoleStore.getState().commandInputBuffer).toBe("first");
    });

    it("navigates down back toward newest", () => {
      useApiConsoleStore.setState({
        commandHistory: ["first", "second", "third"],
      });
      // Go to oldest
      useApiConsoleStore.getState().navigateHistory("up");
      useApiConsoleStore.getState().navigateHistory("up");
      useApiConsoleStore.getState().navigateHistory("up");
      expect(useApiConsoleStore.getState().commandInputBuffer).toBe("first");

      // Go back down
      useApiConsoleStore.getState().navigateHistory("down");
      expect(useApiConsoleStore.getState().commandInputBuffer).toBe("second");
    });

    it("clears buffer when navigating past newest entry", () => {
      useApiConsoleStore.setState({
        commandHistory: ["first"],
      });
      useApiConsoleStore.getState().navigateHistory("up");
      expect(useApiConsoleStore.getState().commandInputBuffer).toBe("first");

      useApiConsoleStore.getState().navigateHistory("down");
      expect(useApiConsoleStore.getState().historyIndex).toBe(-1);
      expect(useApiConsoleStore.getState().commandInputBuffer).toBe("");
    });

    it("down does nothing when already at newest (index -1)", () => {
      useApiConsoleStore.setState({
        commandHistory: ["first"],
        historyIndex: -1,
      });
      useApiConsoleStore.getState().navigateHistory("down");
      expect(useApiConsoleStore.getState().historyIndex).toBe(-1);
    });
  });
});
