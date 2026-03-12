/**
 * Permission checker form: evaluate a tool permission against a manifest.
 *
 * Shows structured evaluation result with the manifest's entries,
 * highlighting which glob pattern matched the tool name (first-match-wins semantics).
 * Fetches manifest detail on evaluate to ensure entries are available
 * (the list endpoint returns summaries without entries).
 *
 * Tab cycles between Manifest ID and Tool Name fields.
 * Enter evaluates using the store's checkPermission().
 * Escape cancels and returns to normal mode.
 */

import React, { useState, useCallback } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import type { PermissionCheck, ManifestEntry, GovernanceCheckResult } from "../../stores/access-store.js";
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

/** Simple glob match (fnmatch-style) for display purposes — mirrors evaluator.py semantics. */
function globMatches(pattern: string, name: string): boolean {
  const regex = new RegExp(
    "^" +
      pattern
        .replace(/[.+^${}()|[\]\\]/g, "\\$&")
        .replace(/\*/g, ".*")
        .replace(/\?/g, ".") +
      "$",
  );
  return regex.test(name);
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
  const fetchManifestDetail = useAccessStore((s) => s.fetchManifestDetail);
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
      const id = manifestId.trim();
      fetchManifestDetail(id, client);
      checkPermission(id, toolName.trim(), client);
    }
    // Governance edge check (requires both agent IDs)
    if (fromAgentId.trim() && toAgentId.trim()) {
      checkGovernanceEdge(fromAgentId.trim(), toAgentId.trim(), zoneId, client);
    }
  }, [client, manifestId, toolName, fromAgentId, toAgentId, zoneId, checkPermission, fetchManifestDetail, checkGovernanceEdge]);

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

  // Look up the manifest (with entries loaded via fetchManifestDetail)
  const matchedManifest = lastResult
    ? manifests.find((m) => m.manifest_id === lastResult.manifest_id)
    : null;

  const entries = matchedManifest?.entries;

  // Find which entry matched (first-match-wins)
  const findMatchedEntry = (
    allEntries: readonly ManifestEntry[],
    tool: string,
  ): ManifestEntry | null => {
    for (const entry of allEntries) {
      if (globMatches(entry.tool_pattern, tool)) {
        return entry;
      }
    }
    return null;
  };

  const matchedEntry = entries && lastResult
    ? findMatchedEntry(entries, lastResult.tool_name)
    : null;

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

      {/* Structured result display */}
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

          {/* Matched pattern */}
          {matchedEntry && (
            <box height={1} width="100%">
              <text>
                {`Matched: pattern="${matchedEntry.tool_pattern}" permission=${matchedEntry.permission}${matchedEntry.max_calls_per_minute ? ` rate=${matchedEntry.max_calls_per_minute}/min` : ""}`}
              </text>
            </box>
          )}

          {entries && !matchedEntry && (
            <box height={1} width="100%">
              <text>{"No entry matched (default: deny)"}</text>
            </box>
          )}

          {/* Manifest entry table (decision trace) */}
          {entries && entries.length > 0 && (
            <>
              <box height={1} width="100%">
                <text>{"--- Manifest Entries (first-match-wins) ---"}</text>
              </box>
              {entries.map((entry, i) => {
                const isMatch = matchedEntry === entry;
                const prefix = isMatch ? ">> " : "   ";
                const rateStr = entry.max_calls_per_minute
                  ? `  rate=${entry.max_calls_per_minute}/min`
                  : "";
                return (
                  <box key={`${entry.tool_pattern}-${i}`} height={1} width="100%">
                    <text>
                      {`${prefix}${entry.tool_pattern.padEnd(30)} ${entry.permission.padEnd(6)}${rateStr}`}
                    </text>
                  </box>
                );
              })}
            </>
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
