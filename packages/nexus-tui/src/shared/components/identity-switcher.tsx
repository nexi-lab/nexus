/**
 * Overlay dialog for switching the active runtime identity.
 *
 * Activated via Ctrl+I from the main app. Provides three editable fields
 * (Agent ID, Subject, Zone ID) pre-filled from the current config.
 * Tab cycles fields, Enter confirms, Escape cancels.
 *
 * On confirm, calls setIdentity() then testConnection() to verify the
 * new credentials against the server.
 */

import { createSignal, Show } from "solid-js";
import type { JSX } from "solid-js";
import { useGlobalStore } from "../../stores/global-store.js";
import { useKeyboard } from "../hooks/use-keyboard.js";

interface IdentitySwitcherProps {
  readonly visible: boolean;
  readonly onClose: () => void;
}

type FieldName = "agentId" | "subject" | "zoneId";

const FIELD_ORDER: readonly FieldName[] = ["agentId", "subject", "zoneId"];

const FIELD_LABELS: Readonly<Record<FieldName, string>> = {
  agentId: "Agent ID",
  subject: "Subject",
  zoneId: "Zone ID",
};

export function IdentitySwitcher(props: IdentitySwitcherProps): JSX.Element {
  const [activeField, setActiveField] = createSignal<FieldName>("agentId");
  const [fields, setFields] = createSignal<Readonly<Record<FieldName, string>>>({
    agentId: "",
    subject: "",
    zoneId: "",
  });

  // Reset fields to current config values when the dialog opens
  const resetFields = () => {
    const currentConfig = useGlobalStore.getState().config;
    setFields({
      agentId: currentConfig.agentId ?? "",
      subject: currentConfig.subject ?? "",
      zoneId: currentConfig.zoneId ?? "",
    });
    setActiveField("agentId");
  };

  const handleConfirm = () => {
    const store = useGlobalStore.getState();
    // Pass all fields explicitly -- empty string becomes undefined to clear the header
    store.setIdentity({
      agentId: fields().agentId.trim() || undefined,
      subject: fields().subject.trim() || undefined,
      zoneId: fields().zoneId.trim() || undefined,
    });
    store.testConnection();
    props.onClose();
  };

  const handleCancel = () => {
    resetFields();
    props.onClose();
  };

  const handleTab = () => {
    const currentIdx = FIELD_ORDER.indexOf(activeField());
    const nextIdx = (currentIdx + 1) % FIELD_ORDER.length;
    const nextField = FIELD_ORDER[nextIdx];
    if (nextField) {
      setActiveField(nextField);
    }
  };

  const handleBackspace = () => {
    setFields((prev) => ({
      ...prev,
      [activeField()]: prev[activeField()].slice(0, -1),
    }));
  };

  const handleUnhandledKey = (keyName: string) => {
    if (!props.visible) return;
    // Single printable character
    if (keyName.length === 1) {
      setFields((prev) => ({
        ...prev,
        [activeField()]: prev[activeField()] + keyName,
      }));
    } else if (keyName === "space") {
      setFields((prev) => ({
        ...prev,
        [activeField()]: prev[activeField()] + " ",
      }));
    }
  };

  useKeyboard(
    (): Record<string, () => void> => props.visible
      ? {
          return: handleConfirm,
          escape: handleCancel,
          tab: handleTab,
          backspace: handleBackspace,
        }
      : {},
    () => props.visible ? handleUnhandledKey : undefined,
  );

  const currentConfig = () => useGlobalStore.getState().config;

  return (
    <Show when={props.visible}>
      <box
        height="100%"
        width="100%"
        justifyContent="center"
        alignItems="center"
      >
        <box
          flexDirection="column"
          borderStyle="double"
          width={60}
          height={11}
          padding={1}
        >
          <text>{"Switch Identity (Tab:next  Enter:confirm  Esc:cancel)"}</text>
          <text>{""}</text>

          {FIELD_ORDER.map((field) => {
            const isActive = () => field === activeField();
            const label = FIELD_LABELS[field];
            const value = () => fields()[field];
            const cursor = () => isActive() ? "\u2588" : "";
            const prefix = () => isActive() ? "\u25b8 " : "  ";
            return (
              <box height={1} width="100%">
                <text>{`${prefix()}${label}: ${value()}${cursor()}`}</text>
              </box>
            );
          })}

          <text>{""}</text>
          <text>{"Current: " + formatCurrentIdentity(currentConfig())}</text>
        </box>
      </box>
    </Show>
  );
}

function formatCurrentIdentity(config: {
  readonly agentId?: string;
  readonly subject?: string;
  readonly zoneId?: string;
}): string {
  const parts: string[] = [];
  if (config.agentId) parts.push(`agent:${config.agentId}`);
  if (config.subject) parts.push(`sub:${config.subject}`);
  if (config.zoneId) parts.push(`zone:${config.zoneId}`);
  return parts.length > 0 ? parts.join(" | ") : "(none)";
}
