/**
 * Shared text input component for consistent input behavior.
 *
 * Handles: character input, backspace, enter (submit), escape (cancel).
 * Can be used standalone or composed in forms.
 *
 * @see Issue #3066, Phase E7
 */

import React from "react";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { statusColor } from "../theme.js";

interface TextInputProps {
  /** Current input value */
  readonly value: string;
  /** Called when value changes */
  readonly onChange: (value: string) => void;
  /** Called when Enter is pressed */
  readonly onSubmit?: (value: string) => void;
  /** Called when Escape is pressed */
  readonly onCancel?: () => void;
  /** Label shown before the input */
  readonly label?: string;
  /** Placeholder text when empty */
  readonly placeholder?: string;
  /** Whether input is active (receives key events) */
  readonly active?: boolean;
}

export function TextInput({
  value,
  onChange,
  onSubmit,
  onCancel,
  label,
  placeholder,
  active = true,
}: TextInputProps): React.ReactNode {
  useKeyboard(
    active
      ? {
          return: () => onSubmit?.(value),
          escape: () => onCancel?.(),
          backspace: () => onChange(value.slice(0, -1)),
        }
      : {},
    active
      ? (key) => {
          // Only append printable characters (single char)
          if (key.length === 1) {
            onChange(value + key);
          }
        }
      : undefined,
  );

  const displayValue = value || (placeholder ? placeholder : "");
  const isDimmed = !value && placeholder;

  return (
    <box flexDirection="row" height={1}>
      {label && (
        <text foregroundColor={statusColor.info}>{`${label}: `}</text>
      )}
      <text dimColor={!!isDimmed}>
        {displayValue}
        {active && <span foregroundColor={statusColor.info}>{"█"}</span>}
      </text>
    </box>
  );
}
