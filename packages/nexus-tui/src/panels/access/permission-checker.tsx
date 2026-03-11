/**
 * Permission checker form: evaluate a tool permission against a manifest.
 *
 * Tab cycles between Manifest ID and Tool Name fields.
 * Enter evaluates using the store's checkPermission().
 * Escape cancels and returns to normal mode.
 */

import React, { useState, useCallback } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import type { PermissionCheck } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

type ActiveField = "manifestId" | "toolName";

interface PermissionCheckerProps {
  readonly initialManifestId: string;
  readonly lastResult: PermissionCheck | null;
  readonly loading: boolean;
  readonly onClose: () => void;
}

export function PermissionChecker({
  initialManifestId,
  lastResult,
  loading,
  onClose,
}: PermissionCheckerProps): React.ReactNode {
  const client = useApi();
  const checkPermission = useAccessStore((s) => s.checkPermission);

  const [manifestId, setManifestId] = useState(initialManifestId);
  const [toolName, setToolName] = useState("");
  const [activeField, setActiveField] = useState<ActiveField>("toolName");

  const handleSubmit = useCallback(() => {
    if (!client || !manifestId.trim() || !toolName.trim()) return;
    checkPermission(manifestId.trim(), toolName.trim(), client);
  }, [client, manifestId, toolName, checkPermission]);

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

      {/* Result display */}
      {lastResult && !loading && (
        <box height={3} width="100%" flexDirection="column">
          <box height={1} width="100%">
            <text>{"--- Result ---"}</text>
          </box>
          <box height={1} width="100%">
            <text>
              {`tool=${lastResult.tool_name}  permission=${lastResult.permission}  agent=${lastResult.agent_id}  manifest=${lastResult.manifest_id}`}
            </text>
          </box>
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
