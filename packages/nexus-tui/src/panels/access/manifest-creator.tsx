/**
 * Manifest creator form: create a new access manifest with a single entry.
 *
 * Tab cycles between fields.
 * Enter submits via the store's createManifest().
 * Escape cancels and returns to normal mode.
 */

import React, { useState, useCallback } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

type ActiveField =
  | "agentId"
  | "name"
  | "toolPattern"
  | "permission"
  | "maxCallsPerMinute"
  | "validFrom"
  | "validUntil";

const FIELD_ORDER: readonly ActiveField[] = [
  "agentId",
  "name",
  "toolPattern",
  "permission",
  "maxCallsPerMinute",
  "validFrom",
  "validUntil",
];

interface ManifestCreatorProps {
  readonly onClose: () => void;
}

export function ManifestCreator({ onClose }: ManifestCreatorProps): React.ReactNode {
  const client = useApi();
  const createManifest = useAccessStore((s) => s.createManifest);
  const manifestsLoading = useAccessStore((s) => s.manifestsLoading);
  const error = useAccessStore((s) => s.error);

  const [agentId, setAgentId] = useState("");
  const [name, setName] = useState("");
  const [toolPattern, setToolPattern] = useState("");
  const [permission, setPermission] = useState("allow");
  const [maxCallsPerMinute, setMaxCallsPerMinute] = useState("");
  const [validFrom, setValidFrom] = useState("");
  const [validUntil, setValidUntil] = useState("");
  const [activeField, setActiveField] = useState<ActiveField>("agentId");
  const [submitted, setSubmitted] = useState(false);

  const setters: Readonly<Record<ActiveField, (fn: (b: string) => string) => void>> = {
    agentId: (fn) => setAgentId((b) => fn(b)),
    name: (fn) => setName((b) => fn(b)),
    toolPattern: (fn) => setToolPattern((b) => fn(b)),
    permission: (fn) => setPermission((b) => fn(b)),
    maxCallsPerMinute: (fn) => setMaxCallsPerMinute((b) => fn(b)),
    validFrom: (fn) => setValidFrom((b) => fn(b)),
    validUntil: (fn) => setValidUntil((b) => fn(b)),
  };

  const handleSubmit = useCallback(() => {
    if (!client || !agentId.trim() || !name.trim() || !toolPattern.trim()) return;
    const maxCalls = maxCallsPerMinute.trim() ? parseInt(maxCallsPerMinute.trim(), 10) : undefined;
    const entry: { tool_pattern: string; permission: string; max_calls_per_minute?: number } = {
      tool_pattern: toolPattern.trim(),
      permission: permission.trim() || "allow",
    };
    if (Number.isFinite(maxCalls)) {
      entry.max_calls_per_minute = maxCalls;
    }
    createManifest(
      {
        agent_id: agentId.trim(),
        name: name.trim(),
        entries: [entry],
        valid_from: validFrom.trim() || undefined,
        valid_until: validUntil.trim() || undefined,
      },
      client,
    );
    setSubmitted(true);
  }, [client, agentId, name, toolPattern, permission, maxCallsPerMinute, validFrom, validUntil, createManifest]);

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

  const fields: readonly { readonly key: ActiveField; readonly label: string; readonly value: string; readonly hint?: string }[] = [
    { key: "agentId", label: "Agent ID       ", value: agentId },
    { key: "name", label: "Manifest Name  ", value: name },
    { key: "toolPattern", label: "Tool Pattern   ", value: toolPattern, hint: "e.g. tool:* or tool:read" },
    { key: "permission", label: "Permission     ", value: permission, hint: "allow|deny" },
    { key: "maxCallsPerMinute", label: "Max Calls/Min  ", value: maxCallsPerMinute, hint: "blank=unlimited" },
    { key: "validFrom", label: "Valid From     ", value: validFrom, hint: "ISO 8601, blank=now" },
    { key: "validUntil", label: "Valid Until    ", value: validUntil, hint: "ISO 8601, blank=none" },
  ];

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Form fields */}
      {fields.map((f) => (
        <box key={f.key} height={1} width="100%">
          <text>
            {activeField === f.key
              ? `> ${f.label}: ${f.value}${cursor}${f.hint ? `  (${f.hint})` : ""}`
              : `  ${f.label}: ${f.value}${f.hint && !f.value ? `  (${f.hint})` : ""}`}
          </text>
        </box>
      ))}

      {manifestsLoading && (
        <box height={1} width="100%">
          <text>Creating manifest...</text>
        </box>
      )}

      {error && !manifestsLoading && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {submitted && !manifestsLoading && !error && (
        <box height={1} width="100%">
          <text>Manifest created successfully. Press Escape to close.</text>
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
