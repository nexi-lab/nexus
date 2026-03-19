/**
 * Permission checker form: evaluate a tool permission against a manifest.
 *
 * Uses the server-side evaluation trace (proof tree) from the backend
 * evaluate endpoint. Shows which manifest entries were checked, which
 * one matched (first-match-wins), and the final decision.
 *
 * Tab cycles between fields.
 * Enter evaluates using the store's checkPermission().
 * Escape cancels and returns to normal mode.
 */

import React, { useState, useCallback } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import type { PermissionCheck, GovernanceCheckResult } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

type ActiveField = "manifestId" | "toolName" | "fromAgentId" | "toAgentId";

interface PermissionCheckerProps {
  readonly initialManifestId: string;
  readonly lastResult: PermissionCheck | null;
  readonly loading: boolean;
  readonly governanceCheck: GovernanceCheckResult | null;
  readonly governanceCheckLoading: boolean;
  readonly zoneId: string | undefined;
  readonly onClose: () => void;
}

export function PermissionChecker({
  initialManifestId,
  lastResult,
  loading,
  governanceCheck,
  governanceCheckLoading,
  zoneId,
  onClose,
}: PermissionCheckerProps): React.ReactNode {
  const client = useApi();
  const checkPermission = useAccessStore((s) => s.checkPermission);
  const checkGovernanceEdge = useAccessStore((s) => s.checkGovernanceEdge);
  const manifests = useAccessStore((s) => s.manifests);

  const [manifestId, setManifestId] = useState(initialManifestId);
  const [toolName, setToolName] = useState("");
  const [fromAgentId, setFromAgentId] = useState("");
  const [toAgentId, setToAgentId] = useState("");
  const [activeField, setActiveField] = useState<ActiveField>("toolName");

  const FIELD_ORDER: readonly ActiveField[] = ["manifestId", "toolName", "fromAgentId", "toAgentId"];

  const setters: Readonly<Record<ActiveField, (fn: (b: string) => string) => void>> = {
    manifestId: (fn) => setManifestId((b) => fn(b)),
    toolName: (fn) => setToolName((b) => fn(b)),
    fromAgentId: (fn) => setFromAgentId((b) => fn(b)),
    toAgentId: (fn) => setToAgentId((b) => fn(b)),
  };

  const handleSubmit = useCallback(() => {
    if (!client) return;
    // Manifest permission check (requires both fields)
    if (manifestId.trim() && toolName.trim()) {
      checkPermission(manifestId.trim(), toolName.trim(), client);
    }
    // Governance edge check (requires both agent IDs)
    if (fromAgentId.trim() && toAgentId.trim()) {
      checkGovernanceEdge(fromAgentId.trim(), toAgentId.trim(), zoneId, client);
    }
  }, [client, manifestId, toolName, fromAgentId, toAgentId, zoneId, checkPermission, checkGovernanceEdge]);

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

  // Look up manifest metadata for display
  const matchedManifest = lastResult
    ? manifests.find((m) => m.manifest_id === lastResult.manifest_id)
    : null;

  // Server-side trace from the evaluate endpoint
  const trace = lastResult?.trace ?? null;

  const fields: readonly { readonly key: ActiveField; readonly label: string; readonly value: string; readonly hint?: string }[] = [
    { key: "manifestId", label: "Manifest ID  ", value: manifestId },
    { key: "toolName", label: "Tool Name    ", value: toolName },
    { key: "fromAgentId", label: "From Agent ID", value: fromAgentId, hint: "governance check" },
    { key: "toAgentId", label: "To Agent ID  ", value: toAgentId, hint: "governance check" },
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

      {/* Loading indicator */}
      {(loading || governanceCheckLoading) && (
        <box height={1} width="100%">
          <text>Evaluating...</text>
        </box>
      )}

      {/* Structured result display — server-side proof tree */}
      {lastResult && !loading && (
        <box flexGrow={1} width="100%" flexDirection="column">
          {/* Decision banner */}
          <box height={1} width="100%">
            <text>
              {lastResult.permission === "allow"
                ? `[ALLOW] ${lastResult.tool_name} -> agent=${lastResult.agent_id}`
                : `[DENY]  ${lastResult.tool_name} -> agent=${lastResult.agent_id}`}
            </text>
          </box>

          {/* Server-side evaluation trace (proof tree) */}
          {trace && trace.entries.length > 0 && (
            <>
              <box height={1} width="100%">
                <text>{"--- Evaluation Trace (server-side, first-match-wins) ---"}</text>
              </box>
              {trace.entries.map((entry) => {
                const prefix = entry.matched && entry.index === trace.matched_index ? ">> " : "   ";
                const matchLabel = entry.matched ? (entry.index === trace.matched_index ? "MATCH" : "match") : "     ";
                const rateStr = entry.max_calls_per_minute
                  ? `  rate=${entry.max_calls_per_minute}/min`
                  : "";
                return (
                  <box key={`trace-${entry.index}`} height={1} width="100%">
                    <text>
                      {`${prefix}[${matchLabel}] ${entry.tool_pattern.padEnd(30)} ${entry.permission.padEnd(6)}${rateStr}`}
                    </text>
                  </box>
                );
              })}
            </>
          )}

          {/* Default deny notice */}
          {trace?.default_applied && (
            <box height={1} width="100%">
              <text>{"No entry matched -> default DENY applied"}</text>
            </box>
          )}

          {/* Matched entry summary */}
          {trace && trace.matched_index >= 0 && trace.entries[trace.matched_index] && (
            <box height={1} width="100%">
              <text>
                {`Deciding entry #${trace.matched_index}: pattern="${trace.entries[trace.matched_index]!.tool_pattern}" permission=${trace.entries[trace.matched_index]!.permission}`}
              </text>
            </box>
          )}

          {/* Manifest metadata */}
          {matchedManifest && (
            <box height={1} width="100%">
              <text>
                {`Manifest: ${matchedManifest.name}  status=${matchedManifest.status}  zone=${matchedManifest.zone_id}`}
              </text>
            </box>
          )}
        </box>
      )}

      {/* Governance edge check result */}
      {governanceCheck && !governanceCheckLoading && (
        <box flexDirection="column" width="100%">
          <box height={1} width="100%">
            <text>{"--- Governance Edge Check ---"}</text>
          </box>
          <box height={1} width="100%">
            <text>
              {governanceCheck.allowed
                ? `[ALLOWED] ${governanceCheck.reason}`
                : `[BLOCKED] ${governanceCheck.reason}`}
            </text>
          </box>
          {governanceCheck.constraint_type && (
            <box height={1} width="100%">
              <text>{`  Constraint: ${governanceCheck.constraint_type}  edge=${governanceCheck.edge_id}`}</text>
            </box>
          )}
        </box>
      )}

      {/* Help */}
      <box height={1} width="100%">
        <text>
          {"Tab:cycle fields  Enter:evaluate  Escape:cancel  Backspace:delete"}
        </text>
      </box>
    </box>
  );
}
