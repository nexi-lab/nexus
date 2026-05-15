/**
 * Shared text input component for consistent input behavior.
 *
 * Handles: character input, backspace, enter (submit), escape (cancel).
 * Can be used standalone or composed in forms.
 *
 * @see Issue #3066, Phase E7
 */

import { Show } from "solid-js";
import { useKeyboard } from "../hooks/use-keyboard.js";
import { statusColor } from "../theme.js";
import { textStyle } from "../text-style.js";

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

export function TextInput(props: TextInputProps) {
  useKeyboard(
    (props.active ?? true)
      ? {
          return: () => props.onSubmit?.(props.value),
          escape: () => props.onCancel?.(),
          backspace: () => props.onChange(props.value.slice(0, -1)),
        }
      : {},
    (props.active ?? true)
      ? (key) => {
          // Only append printable characters (single char)
          if (key.length === 1) {
            props.onChange(props.value + key);
          }
        }
      : undefined,
  );

  const displayValue = props.value || (props.placeholder ? props.placeholder : "");
  const isDimmed = !props.value && props.placeholder;

  return (
    <box flexDirection="row" height={1}>
      <Show when={props.label}>
        <text style={textStyle({ fg: statusColor.info })}>{`${props.label}: `}</text>
      </Show>
      <text style={textStyle({ dim: !!isDimmed })}>
        {displayValue}
        <Show when={props.active ?? true}>
          <span style={textStyle({ fg: statusColor.info })}>{"█"}</span>
        </Show>
      </text>
    </box>
  );
}
