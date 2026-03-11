/**
 * Keyboard navigation hook wrapping OpenTUI's useKeyboard.
 *
 * Provides a simple key-name -> handler abstraction with cleanup on unmount.
 */

import { useCallback, useRef } from "react";
import { useKeyboard as useOpenTuiKeyboard } from "@opentui/react";
import type { KeyEvent } from "@opentui/core";

export type KeyHandler = () => void;
export type KeyBindings = Readonly<Record<string, KeyHandler>>;

/**
 * Register keyboard shortcut handlers.
 *
 * Key format matches OpenTUI key event names:
 * - Letters: "a", "b", ..., "z"
 * - Numbers: "1", "2", "3"
 * - Navigation: "up", "down", "left", "right"
 * - Actions: "return", "escape", "tab", "space", "backspace"
 * - Modified keys: prefix with "ctrl+" or "shift+" (e.g. "ctrl+c")
 *
 * Bindings are cleaned up automatically on unmount.
 */
export function useKeyboard(bindings: KeyBindings): void {
  const bindingsRef = useRef(bindings);
  bindingsRef.current = bindings;

  const handler = useCallback((key: KeyEvent) => {
    // Build normalized key string
    let keyStr = key.name;
    if (key.ctrl) keyStr = `ctrl+${keyStr}`;
    if (key.shift) keyStr = `shift+${keyStr}`;
    if (key.meta) keyStr = `meta+${keyStr}`;

    const fn = bindingsRef.current[keyStr];
    if (fn) {
      fn();
    }
  }, []);

  useOpenTuiKeyboard(handler);
}
