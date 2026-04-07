import { createEffect, createMemo, createSignal, For, Show } from "solid-js";
import { statusColor } from "../theme.js";
import { textStyle } from "../text-style.js";
import { useKeyboard, type KeyBindings } from "../hooks/use-keyboard.js";
import {
  filterCommandPaletteItems,
  type CommandPaletteItem,
} from "../command-palette.js";

interface CommandPaletteProps {
  readonly visible: boolean;
  readonly commands: readonly CommandPaletteItem[];
  readonly onClose: () => void;
}

export function CommandPalette(props: CommandPaletteProps) {
  const [query, setQuery] = createSignal("");
  const [selectedIndex, setSelectedIndex] = createSignal(0);

  createEffect(() => {
    if (!props.visible) {
      setQuery("");
      setSelectedIndex(0);
    }
  });

  const filtered = createMemo(() => filterCommandPaletteItems(props.commands, query()));

  createEffect(() => {
    setSelectedIndex((prev) => Math.min(prev, Math.max(filtered().length - 1, 0)));
  });

  const executeSelected = () => {
    const selected = filtered()[selectedIndex()];
    if (!selected) return;
    selected.run();
    props.onClose();
  };

  const keyBindings = createMemo((): KeyBindings => {
    if (!props.visible) return {};
    return {
          escape: props.onClose,
          return: executeSelected,
          enter: executeSelected,
          down: () => { setSelectedIndex((prev) => Math.min(prev + 1, Math.max(filtered().length - 1, 0))); },
          up: () => { setSelectedIndex((prev) => Math.max(prev - 1, 0)); },
          j: () => { setSelectedIndex((prev) => Math.min(prev + 1, Math.max(filtered().length - 1, 0))); },
          k: () => { setSelectedIndex((prev) => Math.max(prev - 1, 0)); },
          backspace: () => { setQuery((prev) => prev.slice(0, -1)); },
        };
  });

  useKeyboard(
    keyBindings,
    props.visible
      ? (key) => {
          if (key.length === 1) {
            setQuery((prev) => prev + key);
          } else if (key === "space") {
            setQuery((prev) => prev + " ");
          }
        }
      : undefined,
  );

  if (!props.visible) return null;

  return (
    <box height="100%" width="100%" justifyContent="center" alignItems="flex-start">
      <box flexDirection="column" borderStyle="double" width={72} padding={1} marginTop={2}>
        <text style={textStyle({ bold: true })}>Command Palette</text>
        <text style={textStyle({ fg: statusColor.info })}>{`> ${query()}\u2588`}</text>
        <text style={textStyle({ dim: true })}>Type to filter. Enter runs. Esc closes.</text>
        <text>{""}</text>

        <Show when={filtered().length > 0} fallback={<text style={textStyle({ dim: true })}>No matching commands</text>}>
          <For each={filtered().slice(0, 10)}>
            {(command, index) => {
              const selected = () => index() === selectedIndex();
              return (
                <box height={1} width="100%">
                  <text style={selected() ? textStyle({ inverse: true }) : undefined}>
                    {`${selected() ? "> " : "  "}${command.section.padEnd(8)} ${command.title}${command.hint ? `  [${command.hint}]` : ""}`}
                  </text>
                </box>
              );
            }}
          </For>
        </Show>
      </box>
    </box>
  );
}
