/**
 * Keyboard navigation hook wrapping OpenTUI's useKeyboard.
 *
 * Provides a simple key-name -> handler abstraction with cleanup on unmount.
 *
 * Implementation note: we bypass @opentui/solid's useKeyboard (which
 * defers registration inside onMount / createEffect) and instead register
 * the handler synchronously in the component body via renderer.keyInput.on().
 * This ensures the handler is live immediately, which is required for
 * @opentui/solid's test renderer where user effects (createEffect / onMount)
 * are never flushed.
 */

import { useRenderer } from "@opentui/solid";
import { onCleanup } from "solid-js";
import { isTooSmall } from "../terminal-dimensions.js";
import type { KeyEvent } from "@opentui/core";

export type KeyHandler = () => void;
export type KeyBindings = Readonly<Partial<Record<string, KeyHandler>>>;
type MaybeAccessor<T> = T | (() => T);

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
 *
 * @param bindings - Map of key names to handler functions
 */
export function useKeyboard(
  bindings: MaybeAccessor<KeyBindings>,
): void {
  const renderer = useRenderer();

  const handler = (key: KeyEvent) => {
    if (isTooSmall()) { key.preventDefault(); return; }
    if (key.defaultPrevented) return;
    // Build normalized key string
    let keyStr = key.name;
    if (key.ctrl) keyStr = `ctrl+${keyStr}`;
    if (key.shift) keyStr = `shift+${keyStr}`;
    if (key.meta) keyStr = `meta+${keyStr}`;

    const currentBindings = typeof bindings === "function" ? bindings() : bindings;
    const fn = currentBindings[keyStr];
    if (fn) {
      key.preventDefault();
      fn();
    }
  };

  renderer.keyInput.on("keypress", handler);
  onCleanup(() => {
    renderer.keyInput.off("keypress", handler);
  });
}
