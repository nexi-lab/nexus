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
import type { PermissionCheck, ManifestEntry } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

type ActiveField = "manifestId" | "toolName";

interface PermissionCheckerProps {
  readonly initialManifestId: string;
  readonly lastResult: PermissionCheck | null;
  readonly loading: boolean;
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
  onClose,
}: PermissionCheckerProps): React.ReactNode {
  const client = useApi();
  const checkPermission = useAccessStore((s) => s.checkPermission);
  const fetchManifestDetail = useAccessStore((s) => s.fetchManifestDetail);
  const manifests = useAccessStore((s) => s.manifests);

  const [manifestId, setManifestId] = useState(initialManifestId);
  const [toolName, setToolName] = useState("");
  const [activeField, setActiveField] = useState<ActiveField>("toolName");

  const handleSubmit = useCallback(() => {
    if (!client || !manifestId.trim() || !toolName.trim()) return;
    const id = manifestId.trim();
    // Fetch manifest detail (to get entries) alongside the permission check
    fetchManifestDetail(id, client);
    checkPermission(id, toolName.trim(), client);
  }, [client, manifestId, toolName, checkPermission, fetchManifestDetail]);

  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      if (keyName.length === 1) {
        if (activeField === "manifestId") {
          setManifestId((b) => b + keyName);
        } else {
          setToolName((b) => b + keyName);
        }
      } else if (keyName === "space") {
        if (activeField === "manifestId") {
          setManifestId((b) => b + " ");
        } else {
          setToolName((b) => b + " ");
        }
      }
    },
    [activeField],
  );

  useKeyboard(
    {
      return: handleSubmit,
      escape: onClose,
      backspace: () => {
        if (activeField === "manifestId") {
          setManifestId((b) => b.slice(0, -1));
        } else {
          setToolName((b) => b.slice(0, -1));
        }
      },
      tab: () => {
        setActiveField((f) => (f === "manifestId" ? "toolName" : "manifestId"));
      },
    },
    handleUnhandledKey,
  );

  const manifestCursor = activeField === "manifestId" ? "\u2588" : "";
  const toolCursor = activeField === "toolName" ? "\u2588" : "";

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

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Form fields */}
      <box height={1} width="100%">
        <text>
          {activeField === "manifestId"
            ? `> Manifest ID: ${manifestId}${manifestCursor}`
            : `  Manifest ID: ${manifestId}`}
        </text>
      </box>
      <box height={1} width="100%">
        <text>
          {activeField === "toolName"
            ? `> Tool Name:   ${toolName}${toolCursor}`
            : `  Tool Name:   ${toolName}`}
        </text>
      </box>

      {/* Loading indicator */}
      {loading && (
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

      {/* Help */}
      <box height={1} width="100%">
        <text>
          {"Tab:switch field  Enter:evaluate  Escape:cancel  Backspace:delete"}
        </text>
      </box>
    </box>
  );
}
