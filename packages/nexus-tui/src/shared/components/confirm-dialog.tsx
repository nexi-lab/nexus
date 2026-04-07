import { Show } from "solid-js";
import type { JSX } from "solid-js";
/**
 * Reusable confirmation dialog for destructive actions.
 *
 * Renders a centered modal overlay with a message and Y/N keybindings.
 * Follows the same overlay pattern as IdentitySwitcher.
 */

import { useKeyboard } from "../hooks/use-keyboard.js";

interface ConfirmDialogProps {
  readonly visible: boolean;
  readonly title: string;
  readonly message: string;
  readonly onConfirm: () => void;
  readonly onCancel: () => void;
}

export function ConfirmDialog(props: ConfirmDialogProps): JSX.Element {
  useKeyboard(
    (): Record<string, () => void> => props.visible
      ? {
          y: () => props.onConfirm(),
          return: () => props.onConfirm(),
          n: () => props.onCancel(),
          escape: () => props.onCancel(),
        }
      : {},
  );

  return (
    <Show when={props.visible}>
      <box
        height="100%"
        width="100%"
        justifyContent="center"
        alignItems="center"
      >
        <box
          flexDirection="column"
          borderStyle="double"
          width={50}
          height={7}
          padding={1}
        >
          <text>{props.title}</text>
          <text>{""}</text>
          <text>{props.message}</text>
          <text>{""}</text>
          <text>{"Y:confirm  N/Esc:cancel"}</text>
        </box>
      </box>
    </Show>
  );
}
