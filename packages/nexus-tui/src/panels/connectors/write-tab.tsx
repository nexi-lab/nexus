/**
 * Write tab: template-based YAML write composition with validation.
 *
 * Workflow: select mount → select operation → edit template → submit.
 * Template is generated from the operation schema.
 *
 * Supports inline editing of field values (Enter to edit a line, type to
 * replace, Enter to confirm, Escape to cancel). Commented lines can be
 * uncommented with '#' to enable optional fields.
 *
 * Error display parses backend ValidationError format to show field-level
 * errors, skill doc references, and fix examples.
 */

import React, { useState, useEffect, useCallback } from "react";
import type { FetchClient } from "@nexus-ai-fs/api-client";
import { useConnectorsStore } from "../../stores/connectors-store.js";
import { useConfirmStore } from "../../shared/hooks/use-confirm.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useSwr } from "../../shared/hooks/use-swr.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { LoadingIndicator } from "../../shared/components/loading-indicator.js";
import { generateWriteTemplate } from "./template-generator.js";
import { parseWriteError } from "./error-parser.js";
import { statusColor } from "../../shared/theme.js";
import type { SchemaDoc } from "../../stores/connectors-store.js";

interface WriteTabProps {
  readonly client: FetchClient;
  readonly overlayActive: boolean;
}

type WriteMode = "select-mount" | "select-op" | "edit" | "result";

export function WriteTab({ client, overlayActive }: WriteTabProps): React.ReactNode {
  const mounts = useConnectorsStore((s) => s.mounts);
  const selectedMountIndex = useConnectorsStore((s) => s.selectedWriteMountIndex);
  const selectedOpIndex = useConnectorsStore((s) => s.selectedOperationIndex);
  const writeTemplate = useConnectorsStore((s) => s.writeTemplate);
  const writeResult = useConnectorsStore((s) => s.writeResult);
  const writeLoading = useConnectorsStore((s) => s.writeLoading);

  const setSelectedMountIndex = useConnectorsStore((s) => s.setSelectedWriteMountIndex);
  const setSelectedOpIndex = useConnectorsStore((s) => s.setSelectedOperationIndex);
  const setWriteTemplate = useConnectorsStore((s) => s.setWriteTemplate);
  const submitWrite = useConnectorsStore((s) => s.submitWrite);
  const clearWriteResult = useConnectorsStore((s) => s.clearWriteResult);
  const fetchMounts = useConnectorsStore((s) => s.fetchMounts);

  const confirm = useConfirmStore((s) => s.confirm);

  const [mode, setMode] = useState<WriteMode>("select-mount");
  const [editLine, setEditLine] = useState(0);
  const [lineEditMode, setLineEditMode] = useState(false);
  const [lineEditBuffer, setLineEditBuffer] = useState("");

  const selectedMount = mounts[selectedMountIndex];
  const operations = selectedMount?.operations ?? [];
  const selectedOp = operations[selectedOpIndex];

  // Auto-fetch mounts if empty
  useEffect(() => {
    if (mounts.length === 0) {
      fetchMounts(client);
    }
  }, [client, mounts.length, fetchMounts]);

  // Fetch schema and generate template when operation is selected
  const { data: schemaData } = useSwr<SchemaDoc>(
    selectedMount && selectedOp
      ? `schema-${selectedMount.mount_point}-${selectedOp}`
      : "__disabled__",
    async (signal) => {
      if (!selectedMount || !selectedOp) throw new Error("No selection");
      return client.get<SchemaDoc>(
        `/api/v2/connectors/schema/${selectedMount.mount_point.replace(/^\//, "")}/${selectedOp}`,
        { signal },
      );
    },
    { ttlMs: 300_000, enabled: !!selectedMount && !!selectedOp },
  );

  // Generate template from schema
  useEffect(() => {
    if (schemaData?.content && selectedOp && mode === "edit") {
      const template = generateWriteTemplate(selectedOp, schemaData.content);
      setWriteTemplate(template);
      setEditLine(0);
    }
  }, [schemaData?.content, selectedOp, mode, setWriteTemplate]);

  const templateLines = writeTemplate.split("\n");

  // ---------------------------------------------------------------------------
  // Inline line editing helpers
  // ---------------------------------------------------------------------------

  /** Enter edit mode for the current line's value (after the colon). */
  const startLineEdit = useCallback(() => {
    const line = templateLines[editLine];
    if (!line) return;
    // Extract the value portion after "key:" (skip comments-only and header lines)
    const stripped = line.replace(/^#\s*/, "");
    const colonIdx = stripped.indexOf(":");
    if (colonIdx < 0) return; // not a key: value line
    // Pre-fill buffer with current value (trimmed, without inline comment)
    const rawValue = stripped.substring(colonIdx + 1).split("#")[0]?.trim() ?? "";
    // Strip surrounding quotes for editing comfort
    const unquoted = rawValue.replace(/^["']|["']$/g, "");
    setLineEditBuffer(unquoted);
    setLineEditMode(true);
  }, [templateLines, editLine]);

  /** Commit the edited value back into the template. */
  const commitLineEdit = useCallback(() => {
    const line = templateLines[editLine];
    if (!line) { setLineEditMode(false); return; }
    const stripped = line.replace(/^#\s*/, "");
    const colonIdx = stripped.indexOf(":");
    if (colonIdx < 0) { setLineEditMode(false); return; }
    const key = stripped.substring(0, colonIdx);
    // Preserve inline comment
    const commentMatch = stripped.match(/#\s*.+$/);
    const comment = commentMatch ? `  ${commentMatch[0]}` : "";
    // Build new line (uncommented — editing implies enabling)
    const value = lineEditBuffer.includes(" ") || lineEditBuffer === ""
      ? `"${lineEditBuffer}"`
      : lineEditBuffer;
    const newLine = `${key}: ${value}${comment}`;
    const newLines = [...templateLines];
    newLines[editLine] = newLine;
    setWriteTemplate(newLines.join("\n"));
    setLineEditMode(false);
  }, [templateLines, editLine, lineEditBuffer, setWriteTemplate]);

  /** Toggle comment on/off for the current line. */
  const toggleComment = useCallback(() => {
    const line = templateLines[editLine];
    if (!line) return;
    const newLines = [...templateLines];
    if (line.startsWith("# ")) {
      // Uncomment
      newLines[editLine] = line.substring(2);
    } else if (!line.startsWith("#")) {
      // Comment out
      newLines[editLine] = `# ${line}`;
    }
    setWriteTemplate(newLines.join("\n"));
  }, [templateLines, editLine, setWriteTemplate]);

  const handleSubmit = useCallback(async () => {
    if (!selectedMount || !writeTemplate.trim()) return;
    const ok = await confirm(
      "Submit write operation?",
      `Write to ${selectedMount.mount_point} (${selectedOp}). This may have side effects.`,
    );
    if (!ok) return;
    submitWrite(selectedMount.mount_point, writeTemplate, client);
    setMode("result");
  }, [selectedMount, selectedOp, writeTemplate, submitWrite, client, confirm]);

  // Parse structured error from write result
  const parsedError = writeResult?.error ? parseWriteError(writeResult.error) : null;

  // ---------------------------------------------------------------------------
  // Keyboard bindings
  // ---------------------------------------------------------------------------

  const mountNav = listNavigationBindings({
    getIndex: () => selectedMountIndex,
    setIndex: setSelectedMountIndex,
    getLength: () => mounts.length,
    onSelect: () => {
      if (operations.length > 0) {
        setMode("select-op");
        setSelectedOpIndex(0);
      }
    },
  });

  const opNav = listNavigationBindings({
    getIndex: () => selectedOpIndex,
    setIndex: setSelectedOpIndex,
    getLength: () => operations.length,
    onSelect: () => setMode("edit"),
  });

  useKeyboard(
    overlayActive
      ? {}
      : mode === "select-mount"
        ? {
            ...mountNav,
            r: () => fetchMounts(client),
          }
        : mode === "select-op"
          ? {
              ...opNav,
              escape: () => setMode("select-mount"),
            }
          : mode === "edit" && lineEditMode
            ? {
                // Line editing mode — capture typed characters
                return: commitLineEdit,
                escape: () => { setLineEditMode(false); setLineEditBuffer(""); },
                backspace: () => { setLineEditBuffer((b) => b.slice(0, -1)); },
              }
            : mode === "edit"
              ? {
                  j: () => setEditLine(Math.min(editLine + 1, templateLines.length - 1)),
                  k: () => setEditLine(Math.max(editLine - 1, 0)),
                  down: () => setEditLine(Math.min(editLine + 1, templateLines.length - 1)),
                  up: () => setEditLine(Math.max(editLine - 1, 0)),
                  return: startLineEdit,
                  "ctrl+s": handleSubmit,
                  "#": toggleComment,
                  escape: () => setMode("select-op"),
                }
              : {
                  // result mode
                  escape: () => {
                    clearWriteResult();
                    setMode("select-op");
                  },
                  r: () => {
                    clearWriteResult();
                    setMode("edit");
                  },
                  e: () => {
                    // Jump back to edit with current template to fix errors
                    clearWriteResult();
                    setMode("edit");
                  },
                },
    // onUnhandled: capture typed characters in line edit mode
    (!overlayActive && mode === "edit" && lineEditMode)
      ? (keyName: string) => {
          if (keyName === "space") {
            setLineEditBuffer((b) => b + " ");
          } else if (keyName.length === 1) {
            setLineEditBuffer((b) => b + keyName);
          }
        }
      : undefined,
  );

  return (
    <box flexDirection="column" height="100%" width="100%">
      {/* Breadcrumb */}
      <box height={1} width="100%">
        <text>
          <span
            foregroundColor={mode === "select-mount" ? statusColor.info : statusColor.dim}
            bold={mode === "select-mount"}
          >
            Mount
          </span>
          <span foregroundColor={statusColor.dim}>{" → "}</span>
          <span
            foregroundColor={mode === "select-op" ? statusColor.info : statusColor.dim}
            bold={mode === "select-op"}
          >
            Operation
          </span>
          <span foregroundColor={statusColor.dim}>{" → "}</span>
          <span
            foregroundColor={mode === "edit" ? statusColor.info : statusColor.dim}
            bold={mode === "edit"}
          >
            Edit
          </span>
          <span foregroundColor={statusColor.dim}>{" → "}</span>
          <span
            foregroundColor={mode === "result" ? statusColor.info : statusColor.dim}
            bold={mode === "result"}
          >
            Result
          </span>
        </text>
      </box>

      {/* Content area */}
      <box flexGrow={1} borderStyle="single" marginTop={1} flexDirection="column">
        {mode === "select-mount" && (
          <box flexDirection="column" width="100%">
            <box height={1} width="100%">
              <text bold>Select a mount to write to:</text>
            </box>
            {mounts.length === 0 ? (
              <box height={1} width="100%">
                <text foregroundColor={statusColor.dim}>No mounts available.</text>
              </box>
            ) : (
              mounts.map((m, i) => (
                <box key={m.mount_point} height={1} width="100%">
                  <text>
                    <span foregroundColor={i === selectedMountIndex ? statusColor.info : undefined}>
                      {i === selectedMountIndex ? "▶ " : "  "}
                    </span>
                    <span foregroundColor={statusColor.reference}>{m.mount_point}</span>
                    {m.readonly && (
                      <span foregroundColor={statusColor.error}>{" (read-only)"}</span>
                    )}
                    {m.operations.length > 0 && (
                      <span foregroundColor={statusColor.dim}>
                        {`  ${m.operations.length} operations`}
                      </span>
                    )}
                  </text>
                </box>
              ))
            )}
          </box>
        )}

        {mode === "select-op" && (
          <box flexDirection="column" width="100%">
            <box height={1} width="100%">
              <text bold>
                {`Select operation for ${selectedMount?.mount_point ?? ""}:`}
              </text>
            </box>
            {operations.length === 0 ? (
              <box height={1} width="100%">
                <text foregroundColor={statusColor.dim}>No write operations available for this mount.</text>
              </box>
            ) : (
              operations.map((op, i) => (
                <box key={op} height={1} width="100%">
                  <text>
                    <span foregroundColor={i === selectedOpIndex ? statusColor.info : undefined}>
                      {i === selectedOpIndex ? "▶ " : "  "}
                    </span>
                    <span>{op}</span>
                  </text>
                </box>
              ))
            )}
          </box>
        )}

        {mode === "edit" && (
          <box flexDirection="column" width="100%">
            <box height={1} width="100%">
              <text bold>{`Editing: ${selectedOp} → ${selectedMount?.mount_point}`}</text>
            </box>
            {writeLoading ? (
              <LoadingIndicator message="Submitting..." />
            ) : (
              templateLines.map((line, i) => {
                const isActive = i === editLine;
                const isEditing = isActive && lineEditMode;

                return (
                  <box key={i} height={1} width="100%">
                    <text>
                      <span foregroundColor={statusColor.dim}>
                        {String(i + 1).padStart(3, " ")}
                      </span>
                      <span foregroundColor={isActive ? statusColor.info : undefined}>
                        {isActive ? " ▶ " : "   "}
                      </span>
                      {isEditing ? (
                        // Show the key + editable value with cursor
                        (() => {
                          const stripped = line.replace(/^#\s*/, "");
                          const colonIdx = stripped.indexOf(":");
                          const key = colonIdx >= 0 ? stripped.substring(0, colonIdx) : line;
                          return (
                            <>
                              <span foregroundColor={statusColor.info}>{`${key}: `}</span>
                              <span foregroundColor={statusColor.healthy} bold>
                                {lineEditBuffer}
                              </span>
                              <span foregroundColor={statusColor.info}>{"\u2588"}</span>
                            </>
                          );
                        })()
                      ) : (
                        <span
                          foregroundColor={
                            line.startsWith("#")
                              ? statusColor.dim
                              : undefined
                          }
                        >
                          {line}
                        </span>
                      )}
                    </text>
                  </box>
                );
              })
            )}
          </box>
        )}

        {mode === "result" && (
          <box flexDirection="column" width="100%">
            <box height={1} width="100%">
              <text bold>Write Result</text>
            </box>
            {writeResult ? (
              writeResult.success ? (
                <>
                  <box height={1} width="100%">
                    <text foregroundColor={statusColor.healthy}>✓ Write successful!</text>
                  </box>
                  {writeResult.content_hash && (
                    <box height={1} width="100%">
                      <text foregroundColor={statusColor.dim}>{`Hash: ${writeResult.content_hash}`}</text>
                    </box>
                  )}
                </>
              ) : parsedError ? (
                // Structured error display with self-correcting hints
                <box flexDirection="column" width="100%">
                  {/* Error code + message */}
                  <box height={1} width="100%">
                    <text>
                      <span foregroundColor={statusColor.error} bold>
                        {parsedError.code ? `[${parsedError.code}] ` : "✕ "}
                      </span>
                      <span foregroundColor={statusColor.error}>{parsedError.message}</span>
                    </text>
                  </box>

                  {/* Field-level errors */}
                  {parsedError.fieldErrors.length > 0 && (
                    <>
                      <box height={1} width="100%" marginTop={1}>
                        <text bold foregroundColor={statusColor.warning}>Field errors:</text>
                      </box>
                      {parsedError.fieldErrors.map(({ field, error }) => (
                        <box key={field} height={1} width="100%">
                          <text>
                            <span foregroundColor={statusColor.warning}>{"  - "}</span>
                            <span foregroundColor={statusColor.info} bold>{field}</span>
                            <span foregroundColor={statusColor.dim}>{": "}</span>
                            <span>{error}</span>
                          </text>
                        </box>
                      ))}
                    </>
                  )}

                  {/* Skill doc reference */}
                  {parsedError.skillRef && (
                    <box height={1} width="100%" marginTop={1}>
                      <text>
                        <span foregroundColor={statusColor.dim}>{"See: "}</span>
                        <span foregroundColor={statusColor.reference}>{parsedError.skillRef}</span>
                      </text>
                    </box>
                  )}

                  {/* Fix example */}
                  {parsedError.fixExample && (
                    <>
                      <box height={1} width="100%" marginTop={1}>
                        <text foregroundColor={statusColor.healthy} bold>Fix:</text>
                      </box>
                      {parsedError.fixExample.split("\n").map((fixLine, i) => (
                        <box key={i} height={1} width="100%">
                          <text foregroundColor={statusColor.dim}>{`  ${fixLine}`}</text>
                        </box>
                      ))}
                    </>
                  )}

                  {/* Action hint */}
                  <box height={1} width="100%" marginTop={1}>
                    <text foregroundColor={statusColor.dim}>
                      Press e to edit template with corrections, Esc to go back
                    </text>
                  </box>
                </box>
              ) : (
                // Unstructured error fallback
                <>
                  <box height={1} width="100%">
                    <text foregroundColor={statusColor.error}>✕ Write failed</text>
                  </box>
                  {writeResult.error && (
                    <box height={1} width="100%">
                      <text foregroundColor={statusColor.error}>{writeResult.error}</text>
                    </box>
                  )}
                </>
              )
            ) : (
              <LoadingIndicator message="Submitting..." />
            )}
          </box>
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text foregroundColor={statusColor.dim}>
          {mode === "select-mount"
            ? "j/k:navigate  Enter:select  r:refresh"
            : mode === "select-op"
              ? "j/k:navigate  Enter:select  Esc:back"
              : mode === "edit" && lineEditMode
                ? "type:edit value  Enter:confirm  Esc:cancel"
                : mode === "edit"
                  ? "j/k:navigate  Enter:edit line  #:toggle comment  Ctrl+S:submit  Esc:back"
                  : "e:edit again  Esc:back to operations"}
        </text>
      </box>
    </box>
  );
}
