import { afterEach, beforeEach, describe, expect, it, mock } from "bun:test";
import { useAnnouncementStore } from "../../src/stores/announcement-store.js";

describe("announcement-store", () => {
  const originalEnv = process.env.NEXUS_TUI_SCREEN_READER_STDERR;
  const originalWrite = process.stderr.write.bind(process.stderr);

  beforeEach(() => {
    useAnnouncementStore.setState({
      message: null,
      level: "info",
      sequence: 0,
    });
    delete process.env.NEXUS_TUI_SCREEN_READER_STDERR;
  });

  afterEach(() => {
    process.env.NEXUS_TUI_SCREEN_READER_STDERR = originalEnv;
    process.stderr.write = originalWrite;
  });

  it("stores the latest announcement", () => {
    useAnnouncementStore.getState().announce(" Connected  ");

    const state = useAnnouncementStore.getState();
    expect(state.message).toBe("Connected");
    expect(state.level).toBe("info");
    expect(state.sequence).toBe(1);
  });

  it("clears the current announcement", () => {
    useAnnouncementStore.getState().announce("Copied to clipboard", "success");
    useAnnouncementStore.getState().clear();

    const state = useAnnouncementStore.getState();
    expect(state.message).toBeNull();
    expect(state.level).toBe("success");
  });

  it("mirrors announcements to stderr when enabled", () => {
    const write = mock(() => true);
    process.env.NEXUS_TUI_SCREEN_READER_STDERR = "1";
    process.stderr.write = write as typeof process.stderr.write;

    useAnnouncementStore.getState().announce("Panel Files");

    expect(write).toHaveBeenCalledWith("Panel Files\n");
  });
});
