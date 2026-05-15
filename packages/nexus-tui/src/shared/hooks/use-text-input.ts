/**
 * Shared hook for text input mode in terminal panels.
 *
 * Encapsulates the repeated pattern of: activate input mode → capture
 * keystrokes into a buffer → submit on Enter → cancel on Escape →
 * delete on Backspace.
 *
 * Panels integrate this with useKeyboard by spreading inputBindings
 * when the input is active, and passing onUnhandled as the second arg.
 *
 * @example
 * ```tsx
 * const textInput = useTextInput({
 *   onSubmit: (val) => doSearch(val),
 * });
 *
 * useKeyboard(
 *   textInput.active
 *     ? textInput.inputBindings
 *     : { ...normalBindings },
 *   textInput.active ? textInput.onUnhandled : undefined,
 * );
 *
 * // Render: textInput.active ? `Search: ${textInput.buffer}█` : "..."
 * // Activate: textInput.activate(existingQuery)
 * ```
 */

import { createSignal } from "solid-js";

export interface UseTextInputOptions {
  /** Called when Enter is pressed. Receives the trimmed buffer value. */
  readonly onSubmit: (value: string) => void;
  /** Called when Escape is pressed. Defaults to no-op. */
  readonly onCancel?: () => void;
  /**
   * Character filter — return true to accept, false to reject.
   * Only called for single printable characters, not "space".
   * Defaults to accepting all characters.
   */
  readonly filter?: (char: string) => boolean;
}

export interface UseTextInputReturn {
  /** Whether input mode is currently active. */
  readonly active: boolean;
  /** Current buffer contents. */
  readonly buffer: string;
  /** Activate input mode with an optional initial value. */
  readonly activate: (initialValue?: string) => void;
  /** Deactivate input mode programmatically (clears buffer). */
  readonly deactivate: () => void;
  /**
   * Keyboard bindings for input mode.
   * Spread into useKeyboard when active.
   * Includes: return, escape, backspace.
   */
  readonly inputBindings: Readonly<Record<string, () => void>>;
  /**
   * Unhandled key handler for capturing printable characters.
   * Pass as the second arg to useKeyboard when active.
   */
  readonly onUnhandled: (keyName: string) => void;
}

export function useTextInput(options: UseTextInputOptions): UseTextInputReturn {
  const [active, setActive] = createSignal(false);
  const [buffer, setBuffer] = createSignal("");

  const activate = (initialValue?: string) => {
    setBuffer(initialValue ?? "");
    setActive(true);
  };

  const deactivate = () => {
    setBuffer("");
    setActive(false);
  };

  const inputBindings: Record<string, () => void> = {
    return: () => {
      setActive(false);
      options.onSubmit(buffer());
    },
    escape: () => {
      setActive(false);
      setBuffer("");
      options.onCancel?.();
    },
    backspace: () => {
      setBuffer((b) => b.slice(0, -1));
    },
  };

  const onUnhandled = (keyName: string) => {
    if (!active()) return;
    if (keyName === "space") {
      setBuffer((b) => b + " ");
    } else if (keyName.length === 1) {
      const filter = options.filter;
      if (!filter || filter(keyName)) {
        setBuffer((b) => b + keyName);
      }
    }
  };

  return {
    get active() {
      return active();
    },
    get buffer() {
      return buffer();
    },
    activate,
    deactivate,
    inputBindings,
    onUnhandled,
  };
}
