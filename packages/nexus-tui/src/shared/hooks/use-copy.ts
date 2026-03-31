/**
 * Hook for clipboard copy with visual feedback.
 * Shows "Copied!" flash in the status area briefly.
 */
import { useState, useCallback, useRef, useEffect } from "react";
import { copyToClipboard } from "../lib/clipboard.js";
import { useAnnouncementStore } from "../../stores/announcement-store.js";
import { formatSuccessAnnouncement } from "../accessibility-announcements.js";

export function useCopy() {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const announce = useAnnouncementStore((s) => s.announce);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const copy = useCallback((text: string) => {
    copyToClipboard(text);
    announce(formatSuccessAnnouncement("Copied to clipboard"), "success");
    setCopied(true);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setCopied(false), 1500);
  }, [announce]);

  return { copy, copied };
}
