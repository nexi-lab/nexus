import React, { useEffect } from "react";
import { useAnnouncementStore } from "../../stores/announcement-store.js";
import { palette, statusColor } from "../theme.js";

const ANNOUNCEMENT_COLORS = {
  info: palette.faint,
  success: statusColor.healthy,
  error: palette.error,
} as const;

export function AnnouncementBar(): React.ReactNode {
  const message = useAnnouncementStore((s) => s.message);
  const level = useAnnouncementStore((s) => s.level);
  const sequence = useAnnouncementStore((s) => s.sequence);
  const clear = useAnnouncementStore((s) => s.clear);

  useEffect(() => {
    if (!message) return;
    const timer = setTimeout(() => clear(), 4000);
    return () => clearTimeout(timer);
  }, [message, sequence, clear]);

  return (
    <box height={1} width="100%">
      {message
        ? <text foregroundColor={ANNOUNCEMENT_COLORS[level]}>{message}</text>
        : <text> </text>}
    </box>
  );
}
