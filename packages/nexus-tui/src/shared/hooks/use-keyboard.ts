/**
 * Hook for registering keyboard shortcuts with cleanup.
 *
 * Note: OpenTUI handles keyboard input through its own event system.
 * This hook provides a framework-agnostic abstraction over keybindings
 * that can be adapted to OpenTUI's input handling.
 */

import { useEffect, useRef } from "react";

export type KeyBindings = Readonly<Record<string, () => void>>;

/**
 * Register keyboard shortcut handlers.
 *
 * Bindings are cleaned up automatically on unmount or when bindings change.
 * Key format matches OpenTUI key event names (e.g. "j", "k", "enter", "tab", "q").
 */
export function useKeyboard(bindings: KeyBindings): void {
  const bindingsRef = useRef(bindings);
  bindingsRef.current = bindings;

  useEffect(() => {
    // OpenTUI's input system will be wired here.
    // For now, this is a placeholder that stores the bindings
    // for consumption by the component tree.
    //
    // In OpenTUI React, input handling is done via the component's
    // onKeyPress or similar props — this hook will bridge to that.
  }, [bindings]);
}
