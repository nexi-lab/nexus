/**
 * Hook for clipboard copy with visual feedback.
 * Shows "Copied!" flash in the status area briefly.
 */
import { createSignal, onCleanup } from "solid-js";
import { copyToClipboard } from "../lib/clipboard.js";
import { useAnnouncementStore } from "../../stores/announcement-store.js";
import { formatSuccessAnnouncement } from "../accessibility-announcements.js";

export function useCopy() {
  const [copied, setCopied] = createSignal(false);
  let timer: ReturnType<typeof setTimeout> | null = null;
  const announce = useAnnouncementStore((s) => s.announce);

  onCleanup(() => {
    if (timer) clearTimeout(timer);
  });

  const copy = (text: string) => {
    copyToClipboard(text);
    announce(formatSuccessAnnouncement("Copied to clipboard"), "success");
    setCopied(true);
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => setCopied(false), 1500);
  };

  return {
    copy,
    get copied() {
      return copied();
    },
  };
}
