/**
 * Transfer form overlay: three-field form (to, amount, memo) for credit transfers.
 *
 * Tab cycles between fields, Enter submits, Escape cancels.
 * Uses the same input-mode pattern as search-panel.tsx.
 */

import React, { useState, useCallback } from "react";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";

type TransferField = "to" | "amount" | "memo";

const FIELD_ORDER: readonly TransferField[] = ["to", "amount", "memo"];
const FIELD_LABELS: Readonly<Record<TransferField, string>> = {
  to: "To",
  amount: "Amount",
  memo: "Memo",
};

interface TransferFormProps {
  readonly onSubmit: (to: string, amount: string, memo: string) => void;
  readonly onCancel: () => void;
}

export function TransferForm({
  onSubmit,
  onCancel,
}: TransferFormProps): React.ReactNode {
  const [activeField, setActiveField] = useState<TransferField>("to");
  const [fields, setFields] = useState<Readonly<Record<TransferField, string>>>({
    to: "",
    amount: "",
    memo: "",
  });

  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      if (keyName.length === 1) {
        setFields((prev) => ({ ...prev, [activeField]: prev[activeField] + keyName }));
      } else if (keyName === "space") {
        setFields((prev) => ({ ...prev, [activeField]: prev[activeField] + " " }));
      }
    },
    [activeField],
  );

  useKeyboard(
    {
      tab: () => {
        const currentIdx = FIELD_ORDER.indexOf(activeField);
        const nextIdx = (currentIdx + 1) % FIELD_ORDER.length;
        const nextField = FIELD_ORDER[nextIdx];
        if (nextField) {
          setActiveField(nextField);
        }
      },
      backspace: () => {
        setFields((prev) => ({
          ...prev,
          [activeField]: prev[activeField].slice(0, -1),
        }));
      },
      return: () => {
        const to = fields.to.trim();
        const amount = fields.amount.trim();
        const memo = fields.memo.trim();
        if (to && amount) {
          onSubmit(to, amount, memo);
        }
      },
      escape: () => {
        onCancel();
      },
    },
    handleUnhandledKey,
  );

  return (
    <box
      height="100%"
      width="100%"
      flexDirection="column"
      borderStyle="single"
    >
      <box height={1} width="100%">
        <text>{"--- Transfer Credits ---"}</text>
      </box>

      {FIELD_ORDER.map((field) => {
        const isActive = field === activeField;
        const label = FIELD_LABELS[field];
        const value = fields[field];
        const cursor = isActive ? "\u2588" : "";
        const prefix = isActive ? "> " : "  ";
        return (
          <box key={field} height={1} width="100%">
            <text>{`${prefix}${label}: ${value}${cursor}`}</text>
          </box>
        );
      })}

      <box height={1} width="100%" marginTop={1}>
        <text>
          {"Tab:next field  Enter:submit  Escape:cancel  Backspace:delete"}
        </text>
      </box>
    </box>
  );
}
