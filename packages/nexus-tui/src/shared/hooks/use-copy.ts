/**
 * Hook for clipboard copy with visual feedback.
 * Shows "Copied!" flash in the status area briefly.
 */
import { useState, useCallback } from "react";
import { copyToClipboard } from "../lib/clipboard.js";

export function useCopy() {
  const [copied, setCopied] = useState(false);

  const copy = useCallback((text: string) => {
    copyToClipboard(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, []);

  return { copy, copied };
}
