/**
 * Hook for clipboard copy with visual feedback.
 * Shows "Copied!" flash in the status area briefly.
 */
import { useState, useCallback, useRef, useEffect } from "react";
import { copyToClipboard } from "../lib/clipboard.js";

export function useCopy() {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const copy = useCallback((text: string) => {
    copyToClipboard(text);
    setCopied(true);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setCopied(false), 1500);
  }, []);

  return { copy, copied };
}
