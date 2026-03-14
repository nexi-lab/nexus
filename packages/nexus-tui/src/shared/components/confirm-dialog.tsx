/**
 * Reusable confirmation dialog for destructive actions.
 *
 * Renders a centered modal overlay with a message and Y/N keybindings.
 * Follows the same overlay pattern as IdentitySwitcher.
 */

import React from "react";
import { useKeyboard } from "../hooks/use-keyboard.js";

interface ConfirmDialogProps {
  readonly visible: boolean;
  readonly title: string;
  readonly message: string;
  readonly onConfirm: () => void;
  readonly onCancel: () => void;
}

export function ConfirmDialog({
  visible,
  title,
  message,
  onConfirm,
  onCancel,
}: ConfirmDialogProps): React.ReactNode {
  useKeyboard(
    visible
      ? {
          y: onConfirm,
          return: onConfirm,
          n: onCancel,
          escape: onCancel,
        }
      : {},
  );

  if (!visible) return null;

  return (
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
        <text>{title}</text>
        <text>{""}</text>
        <text>{message}</text>
        <text>{""}</text>
        <text>{"Y:confirm  N/Esc:cancel"}</text>
      </box>
    </box>
  );
}
