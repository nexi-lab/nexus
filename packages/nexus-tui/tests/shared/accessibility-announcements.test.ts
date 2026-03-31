import { describe, expect, it } from "bun:test";
import {
  formatConnectionAnnouncement,
  formatDirectoryAnnouncement,
  formatErrorAnnouncement,
  formatPanelAnnouncement,
  formatSelectionAnnouncement,
  normalizeAnnouncementMessage,
} from "../../src/shared/accessibility-announcements.js";

describe("accessibility announcements", () => {
  it("normalizes whitespace", () => {
    expect(normalizeAnnouncementMessage("  one\n two\t three  ")).toBe("one two three");
  });

  it("formats panel announcements", () => {
    expect(formatPanelAnnouncement("Files")).toBe("Panel Files");
  });

  it("formats connection status announcements", () => {
    expect(formatConnectionAnnouncement("connected")).toBe("Connected");
    expect(formatConnectionAnnouncement("error", "Server health check failed")).toBe(
      "Connection error: Server health check failed",
    );
  });

  it("formats directory load announcements", () => {
    expect(formatDirectoryAnnouncement("/workspace", 1)).toBe("1 item in /workspace");
    expect(formatDirectoryAnnouncement("/workspace", 12)).toBe("12 items in /workspace");
  });

  it("formats selection announcements", () => {
    expect(formatSelectionAnnouncement("src", true)).toBe("Selected folder src");
    expect(formatSelectionAnnouncement("README.md", false)).toBe("Selected file README.md");
  });

  it("formats error announcements", () => {
    expect(formatErrorAnnouncement("No API key")).toBe("Error: No API key");
  });
});
