import React, { useCallback, useEffect, useMemo, useState } from "react";
import { statusColor } from "../theme.js";
import { textStyle } from "../text-style.js";
import { useKeyboard } from "../hooks/use-keyboard.js";
import {
  filterCommandPaletteItems,
  type CommandPaletteItem,
} from "../command-palette.js";

interface CommandPaletteProps {
  readonly visible: boolean;
  readonly commands: readonly CommandPaletteItem[];
  readonly onClose: () => void;
}

export function CommandPalette({
  visible,
  commands,
  onClose,
}: CommandPaletteProps): React.ReactNode {
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);

  useEffect(() => {
    if (!visible) {
      setQuery("");
      setSelectedIndex(0);
    }
  }, [visible]);

  const filtered = useMemo(
    () => filterCommandPaletteItems(commands, query),
    [commands, query],
  );

  useEffect(() => {
    setSelectedIndex((prev) => Math.min(prev, Math.max(filtered.length - 1, 0)));
  }, [filtered.length]);

  const executeSelected = useCallback(() => {
    const selected = filtered[selectedIndex];
    if (!selected) return;
    selected.run();
    onClose();
  }, [filtered, selectedIndex, onClose]);

  useKeyboard(
    visible
      ? {
          escape: onClose,
          return: executeSelected,
          down: () => setSelectedIndex((prev) => Math.min(prev + 1, Math.max(filtered.length - 1, 0))),
          up: () => setSelectedIndex((prev) => Math.max(prev - 1, 0)),
          j: () => setSelectedIndex((prev) => Math.min(prev + 1, Math.max(filtered.length - 1, 0))),
          k: () => setSelectedIndex((prev) => Math.max(prev - 1, 0)),
          backspace: () => setQuery((prev) => prev.slice(0, -1)),
        }
      : {},
    visible
      ? (key) => {
          if (key.length === 1) {
            setQuery((prev) => prev + key);
          } else if (key === "space") {
            setQuery((prev) => prev + " ");
          }
        }
      : undefined,
  );

  if (!visible) return null;

  return (
    <box height="100%" width="100%" justifyContent="center" alignItems="flex-start">
      <box flexDirection="column" borderStyle="double" width={72} padding={1} marginTop={2}>
        <text style={textStyle({ bold: true })}>Command Palette</text>
        <text style={textStyle({ fg: statusColor.info })}>{`> ${query}\u2588`}</text>
        <text style={textStyle({ dim: true })}>Type to filter. Enter runs. Esc closes.</text>
        <text>{""}</text>

        {filtered.length === 0 ? (
          <text style={textStyle({ dim: true })}>No matching commands</text>
        ) : (
          filtered.slice(0, 10).map((command, index) => {
            const selected = index === selectedIndex;
            return (
              <box key={command.id} height={1} width="100%">
                <text style={selected ? textStyle({ inverse: true }) : undefined}>
                  {`${selected ? "> " : "  "}${command.section.padEnd(8)} ${command.title}${command.hint ? `  [${command.hint}]` : ""}`}
                </text>
              </box>
            );
          })
        )}
      </box>
    </box>
  );
}
