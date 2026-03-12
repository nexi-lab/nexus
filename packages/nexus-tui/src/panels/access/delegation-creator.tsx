/**
 * Delegation creator form: create a new delegation with namespace scope.
 *
 * Tab cycles between fields.
 * Enter submits via the store's createDelegation().
 * Escape cancels and returns to normal mode.
 */

import React, { useState, useCallback } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import type { DelegationCreateResponse } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

type ActiveField =
  | "workerId"
  | "workerName"
  | "namespaceMode"
  | "scopePrefix"
  | "intent"
  | "canSubDelegate"
  | "ttlSeconds";

const FIELD_ORDER: readonly ActiveField[] = [
  "workerId",
  "workerName",
  "namespaceMode",
  "scopePrefix",
  "intent",
  "canSubDelegate",
  "ttlSeconds",
];

interface DelegationCreatorProps {
  readonly onClose: () => void;
}

export function DelegationCreator({ onClose }: DelegationCreatorProps): React.ReactNode {
  const client = useApi();
  const createDelegation = useAccessStore((s) => s.createDelegation);
  const delegationsLoading = useAccessStore((s) => s.delegationsLoading);
  const lastResult = useAccessStore((s) => s.lastDelegationCreate);
  const error = useAccessStore((s) => s.error);

  const [workerId, setWorkerId] = useState("");
  const [workerName, setWorkerName] = useState("");
  const [namespaceMode, setNamespaceMode] = useState("clean");
  const [scopePrefix, setScopePrefix] = useState("");
  const [intent, setIntent] = useState("");
  const [canSubDelegate, setCanSubDelegate] = useState("no");
  const [ttlSeconds, setTtlSeconds] = useState("");
  const [activeField, setActiveField] = useState<ActiveField>("workerId");

  const setters: Readonly<Record<ActiveField, (fn: (b: string) => string) => void>> = {
    workerId: (fn) => setWorkerId((b) => fn(b)),
    workerName: (fn) => setWorkerName((b) => fn(b)),
    namespaceMode: (fn) => setNamespaceMode((b) => fn(b)),
    scopePrefix: (fn) => setScopePrefix((b) => fn(b)),
    intent: (fn) => setIntent((b) => fn(b)),
    canSubDelegate: (fn) => setCanSubDelegate((b) => fn(b)),
    ttlSeconds: (fn) => setTtlSeconds((b) => fn(b)),
  };

  const handleSubmit = useCallback(() => {
    if (!client || !workerId.trim() || !workerName.trim() || !intent.trim()) return;
    const ttl = ttlSeconds.trim() ? parseInt(ttlSeconds.trim(), 10) : undefined;
    createDelegation(
      {
        worker_id: workerId.trim(),
        worker_name: workerName.trim(),
        namespace_mode: namespaceMode.trim() || "clean",
        scope_prefix: scopePrefix.trim() || undefined,
        intent: intent.trim(),
        can_sub_delegate: canSubDelegate.toLowerCase() === "yes",
        ttl_seconds: Number.isFinite(ttl) ? ttl : undefined,
      },
      client,
    );
  }, [client, workerId, workerName, namespaceMode, scopePrefix, intent, canSubDelegate, ttlSeconds, createDelegation]);

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
    { key: "workerId", label: "Worker ID      ", value: workerId },
    { key: "workerName", label: "Worker Name    ", value: workerName },
    { key: "namespaceMode", label: "Namespace Mode ", value: namespaceMode, hint: "copy|clean|shared" },
    { key: "scopePrefix", label: "Scope Prefix   ", value: scopePrefix, hint: "e.g. files/reports/" },
    { key: "intent", label: "Intent         ", value: intent },
    { key: "canSubDelegate", label: "Sub-delegate?  ", value: canSubDelegate, hint: "yes|no" },
    { key: "ttlSeconds", label: "TTL (seconds)  ", value: ttlSeconds, hint: "1-86400, blank=none" },
  ];

  const showResult = lastResult && !delegationsLoading;

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

      {delegationsLoading && (
        <box height={1} width="100%">
          <text>Creating delegation...</text>
        </box>
      )}

      {error && !delegationsLoading && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {showResult && (
        <DelegationCreateResult result={lastResult} />
      )}

      <box height={1} width="100%">
        <text>
          {"Tab:next field  Enter:create  Escape:cancel  Backspace:delete"}
        </text>
      </box>
    </box>
  );
}

function DelegationCreateResult({
  result,
}: {
  readonly result: DelegationCreateResponse;
}): React.ReactNode {
  return (
    <box flexDirection="column" width="100%">
      <box height={1} width="100%">
        <text>{"--- Delegation Created ---"}</text>
      </box>
      <box height={1} width="100%">
        <text>{`  ID:     ${result.delegation_id}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`  Worker: ${result.worker_agent_id}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`  Mode:   ${result.delegation_mode}`}</text>
      </box>
      <box height={1} width="100%">
        <text>{`  Key:    ${result.api_key}`}</text>
      </box>
      {result.expires_at && (
        <box height={1} width="100%">
          <text>{`  Expiry: ${result.expires_at}`}</text>
        </box>
      )}
      {result.mount_table.length > 0 && (
        <>
          <box height={1} width="100%">
            <text>{"  Mount table:"}</text>
          </box>
          {result.mount_table.map((path, i) => (
            <box key={`mt-${i}`} height={1} width="100%">
              <text>{`    ${path}`}</text>
            </box>
          ))}
        </>
      )}
    </box>
  );
}
