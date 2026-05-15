/**
 * Constraint creator form: create a new ReBAC governance constraint.
 *
 * Tab cycles between fields.
 * Enter submits via the access store's createConstraint().
 * Escape cancels and returns to normal mode.
 */

import { createSignal } from "solid-js";
import type { JSX } from "solid-js";
import { useAccessStore } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

type ActiveField = "fromAgentId" | "toAgentId" | "constraintType";

const FIELD_ORDER: readonly ActiveField[] = [
  "fromAgentId",
  "toAgentId",
  "constraintType",
];

interface ConstraintCreatorProps {
  readonly zoneId: string;
  readonly onClose: () => void;
}

export function ConstraintCreator({ zoneId, onClose }: ConstraintCreatorProps): JSX.Element {
  const client = useApi();
  const createConstraint = useAccessStore((s) => s.createConstraint);
  const constraintsLoading = useAccessStore((s) => s.constraintsLoading);
  const error = useAccessStore((s) => s.error);

  const [fromAgentId, setFromAgentId] = createSignal("");
  const [toAgentId, setToAgentId] = createSignal("");
  const [constraintType, setConstraintType] = createSignal("");
  const [activeField, setActiveField] = createSignal<ActiveField>("fromAgentId");

  const setters: Readonly<Record<ActiveField, (fn: (b: string) => string) => void>> = {
    fromAgentId: (fn) => setFromAgentId((b) => fn(b)),
    toAgentId: (fn) => setToAgentId((b) => fn(b)),
    constraintType: (fn) => setConstraintType((b) => fn(b)),
  };

  const handleSubmit = () => {
    if (!client || !fromAgentId().trim() || !toAgentId().trim() || !constraintType().trim()) return;
    createConstraint(
      {
        from_agent_id: fromAgentId().trim(),
        to_agent_id: toAgentId().trim(),
        constraint_type: constraintType().trim(),
        zone_id: zoneId,
      },
      client,
    );
  };

  const handleUnhandledKey = (keyName: string) => {
      const setter = setters[activeField()];
      if (keyName.length === 1) {
        setter((b) => b + keyName);
      } else if (keyName === "space") {
        setter((b) => b + " ");
      }
    };

  useKeyboard(
    {
      return: handleSubmit,
      escape: onClose,
      backspace: () => {
        setters[activeField()]((b) => b.slice(0, -1));
      },
      tab: () => {
        const currentIdx = FIELD_ORDER.indexOf(activeField());
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

  const fields: readonly { readonly key: ActiveField; readonly label: string; readonly value: string; readonly hint?: string }[] = [
    { key: "fromAgentId", label: "From Agent ID   ", value: fromAgentId() },
    { key: "toAgentId", label: "To Agent ID     ", value: toAgentId() },
    { key: "constraintType", label: "Constraint Type ", value: constraintType(), hint: "e.g. deny, rate_limit" },
  ];

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Form fields */}
      {fields.map((f) => (
        <box height={1} width="100%">
          <text>
            {activeField() === f.key
              ? `> ${f.label}: ${f.value}${cursor}${f.hint ? `  (${f.hint})` : ""}`
              : `  ${f.label}: ${f.value}${f.hint && !f.value ? `  (${f.hint})` : ""}`}
          </text>
        </box>
      ))}

      {constraintsLoading && (
        <box height={1} width="100%">
          <text>Creating constraint...</text>
        </box>
      )}

      {error && !constraintsLoading && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      <box height={1} width="100%">
        <text>
          {"Tab:next field  Enter:create  Escape:cancel  Backspace:delete"}
        </text>
      </box>
    </box>
  );
}
