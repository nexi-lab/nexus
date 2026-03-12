/**
 * Dispute filer form: file a new dispute against an exchange.
 *
 * Tab cycles between fields.
 * Enter submits via the store's fileDispute().
 * Escape cancels and returns to normal mode.
 */

import React, { useState, useCallback } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

type ActiveField = "exchangeId" | "complainantId" | "respondentId" | "reason";

const FIELD_ORDER: readonly ActiveField[] = [
  "exchangeId",
  "complainantId",
  "respondentId",
  "reason",
];

interface DisputeFilerProps {
  readonly onClose: () => void;
}

export function DisputeFiler({ onClose }: DisputeFilerProps): React.ReactNode {
  const client = useApi();
  const fileDispute = useAccessStore((s) => s.fileDispute);
  const disputesLoading = useAccessStore((s) => s.disputesLoading);
  const error = useAccessStore((s) => s.error);

  const [exchangeId, setExchangeId] = useState("");
  const [complainantId, setComplainantId] = useState("");
  const [respondentId, setRespondentId] = useState("");
  const [reason, setReason] = useState("");
  const [activeField, setActiveField] = useState<ActiveField>("exchangeId");

  const setters: Readonly<Record<ActiveField, (fn: (b: string) => string) => void>> = {
    exchangeId: (fn) => setExchangeId((b) => fn(b)),
    complainantId: (fn) => setComplainantId((b) => fn(b)),
    respondentId: (fn) => setRespondentId((b) => fn(b)),
    reason: (fn) => setReason((b) => fn(b)),
  };

  const handleSubmit = useCallback(() => {
    if (
      !client ||
      !exchangeId.trim() ||
      !complainantId.trim() ||
      !respondentId.trim() ||
      !reason.trim()
    ) {
      return;
    }
    fileDispute(
      exchangeId.trim(),
      complainantId.trim(),
      respondentId.trim(),
      reason.trim(),
      client,
    );
  }, [client, exchangeId, complainantId, respondentId, reason, fileDispute]);

  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      const setter = setters[activeField];
      if (keyName.length === 1) {
        setter((b) => b + keyName);
      } else if (keyName === "space") {
        setter((b) => b + " ");
      }
    },
    [activeField],
  );

  useKeyboard(
    {
      return: handleSubmit,
      escape: onClose,
      backspace: () => {
        setters[activeField]((b) => b.slice(0, -1));
      },
      tab: () => {
        const currentIdx = FIELD_ORDER.indexOf(activeField);
        const nextIdx = (currentIdx + 1) % FIELD_ORDER.length;
        const next = FIELD_ORDER[nextIdx];
        if (next) {
          setActiveField(next);
        }
      },
    },
    handleUnhandledKey,
  );

  const cursor = "\u2588";

  const fields: readonly { readonly key: ActiveField; readonly label: string; readonly value: string }[] = [
    { key: "exchangeId", label: "Exchange ID   ", value: exchangeId },
    { key: "complainantId", label: "Complainant ID", value: complainantId },
    { key: "respondentId", label: "Respondent ID ", value: respondentId },
    { key: "reason", label: "Reason        ", value: reason },
  ];

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Form fields */}
      {fields.map((f) => (
        <box key={f.key} height={1} width="100%">
          <text>
            {activeField === f.key
              ? `> ${f.label}: ${f.value}${cursor}`
              : `  ${f.label}: ${f.value}`}
          </text>
        </box>
      ))}

      {/* Loading indicator */}
      {disputesLoading && (
        <box height={1} width="100%">
          <text>Filing dispute...</text>
        </box>
      )}

      {/* Error display */}
      {error && !disputesLoading && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {/* Help */}
      <box height={1} width="100%">
        <text>
          {"Tab:next field  Enter:file dispute  Escape:cancel  Backspace:delete"}
        </text>
      </box>
    </box>
  );
}
