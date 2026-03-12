/**
 * Delegation completer form: mark a delegation as completed/failed/timeout.
 *
 * Tab cycles between outcome and quality score fields.
 * Enter submits via the store's completeDelegation().
 * Escape cancels and returns to normal mode.
 */

import React, { useState, useCallback } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

type ActiveField = "outcome" | "qualityScore";

const FIELD_ORDER: readonly ActiveField[] = ["outcome", "qualityScore"];

interface DelegationCompleterProps {
  readonly delegationId: string;
  readonly onClose: () => void;
}

export function DelegationCompleter({
  delegationId,
  onClose,
}: DelegationCompleterProps): React.ReactNode {
  const client = useApi();
  const completeDelegation = useAccessStore((s) => s.completeDelegation);
  const delegationsLoading = useAccessStore((s) => s.delegationsLoading);
  const error = useAccessStore((s) => s.error);

  const [outcome, setOutcome] = useState("completed");
  const [qualityScore, setQualityScore] = useState("");
  const [activeField, setActiveField] = useState<ActiveField>("outcome");

  const setters: Readonly<Record<ActiveField, (fn: (b: string) => string) => void>> = {
    outcome: (fn) => setOutcome((b) => fn(b)),
    qualityScore: (fn) => setQualityScore((b) => fn(b)),
  };

  const handleSubmit = useCallback(() => {
    if (!client || !outcome.trim()) return;
    const score = qualityScore.trim() ? parseFloat(qualityScore.trim()) : null;
    completeDelegation(delegationId, outcome.trim(), score, client);
  }, [client, delegationId, outcome, qualityScore, completeDelegation]);

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

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>{`Complete delegation: ${delegationId}`}</text>
      </box>

      <box height={1} width="100%">
        <text>
          {activeField === "outcome"
            ? `> Outcome:       ${outcome}${cursor}  (completed|failed|timeout)`
            : `  Outcome:       ${outcome}`}
        </text>
      </box>
      <box height={1} width="100%">
        <text>
          {activeField === "qualityScore"
            ? `> Quality Score: ${qualityScore}${cursor}  (0.0-1.0, optional)`
            : `  Quality Score: ${qualityScore}`}
        </text>
      </box>

      {delegationsLoading && (
        <box height={1} width="100%">
          <text>Completing delegation...</text>
        </box>
      )}

      {error && !delegationsLoading && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      <box height={1} width="100%">
        <text>
          {"Tab:next field  Enter:complete  Escape:cancel  Backspace:delete"}
        </text>
      </box>
    </box>
  );
}
