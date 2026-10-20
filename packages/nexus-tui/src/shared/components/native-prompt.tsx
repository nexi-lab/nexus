import { useKeyboard } from "../hooks/use-keyboard.js";
import type { InputRenderable } from "@opentui/core";

export function NativePrompt(props: {
  readonly label: string; readonly initialValue?: string;
  readonly onSubmit: (value: string) => void; readonly onCancel: () => void;
}) {
  let field: InputRenderable | undefined;
  useKeyboard({ escape: props.onCancel });
  return <box flexDirection="column" height={3} width="100%">
    <text>{`${props.label} (Enter:submit  Esc:cancel)`}</text>
    <input ref={(value) => { field = value; }} value={props.initialValue ?? ""} focused
      onSubmit={() => props.onSubmit(field?.value ?? "")} />
  </box>;
}
