/**
 * Inline search/filter input that appears when "/" is pressed.
 */

import React from "react";

interface SearchInputProps {
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly onClose: () => void;
  readonly placeholder?: string;
}

export function SearchInput({
  value,
  placeholder,
}: SearchInputProps): React.ReactNode {
  return (
    <box height={1} width="100%" flexDirection="row">
      <text>/</text>
      <text>{value || placeholder || "Search..."}</text>
    </box>
  );
}
