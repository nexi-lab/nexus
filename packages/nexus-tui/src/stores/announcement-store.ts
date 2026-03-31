import { create } from "zustand";
import {
  normalizeAnnouncementMessage,
  type AnnouncementLevel,
} from "../shared/accessibility-announcements.js";

export interface AnnouncementState {
  readonly message: string | null;
  readonly level: AnnouncementLevel;
  readonly sequence: number;
  readonly announce: (message: string, level?: AnnouncementLevel) => void;
  readonly clear: () => void;
}

function emitAnnouncementToStderr(message: string): void {
  if (process.env.NEXUS_TUI_SCREEN_READER_STDERR !== "1") return;
  try {
    process.stderr.write(`${message}\n`);
  } catch {
    // Best-effort transport only.
  }
}

export const useAnnouncementStore = create<AnnouncementState>((set) => ({
  message: null,
  level: "info",
  sequence: 0,

  announce: (message, level = "info") => {
    const normalized = normalizeAnnouncementMessage(message);
    if (!normalized) return;
    emitAnnouncementToStderr(normalized);
    set((state) => ({
      message: normalized,
      level,
      sequence: state.sequence + 1,
    }));
  },

  clear: () => {
    set((state) => ({
      ...state,
      message: null,
    }));
  },
}));
