/**
 * Namespace config editor: view and edit delegation namespace configuration.
 *
 * Fetches namespace detail from the backend endpoint and displays
 * the delegation mode, scope constraints, grant modifications, and
 * current mount table (visible paths).
 *
 * Press 'e' to enter edit mode, which allows editing scope_prefix,
 * adding/removing grants, and adding/removing readonly paths.
 * Tab cycles between editable fields, Enter saves, Escape cancels.
 */

import React, { useState, useEffect, useCallback } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

type EditField = "scopePrefix" | "addGrant" | "removeGrant" | "readonlyPath";

const EDIT_FIELD_ORDER: readonly EditField[] = [
  "scopePrefix",
  "addGrant",
  "removeGrant",
  "readonlyPath",
];

interface NamespaceConfigViewProps {
  readonly delegationId: string;
  readonly onClose: () => void;
}

export function NamespaceConfigView({
  delegationId,
  onClose,
}: NamespaceConfigViewProps): React.ReactNode {
  const client = useApi();
  const namespaceDetail = useAccessStore((s) => s.namespaceDetail);
  const namespaceDetailLoading = useAccessStore((s) => s.namespaceDetailLoading);
  const error = useAccessStore((s) => s.error);
  const fetchNamespaceDetail = useAccessStore((s) => s.fetchNamespaceDetail);
  const updateNamespaceConfig = useAccessStore((s) => s.updateNamespaceConfig);

  const [editing, setEditing] = useState(false);
  const [activeField, setActiveField] = useState<EditField>("scopePrefix");
  const [scopePrefix, setScopePrefix] = useState("");
  const [addGrant, setAddGrant] = useState("");
  const [removeGrant, setRemoveGrant] = useState("");
  const [readonlyPath, setReadonlyPath] = useState("");

  useEffect(() => {
    if (client && delegationId) {
      fetchNamespaceDetail(delegationId, client);
    }
  }, [client, delegationId, fetchNamespaceDetail]);

  // Populate edit fields from fetched data
  useEffect(() => {
    if (namespaceDetail) {
      setScopePrefix(namespaceDetail.scope_prefix ?? "");
    }
  }, [namespaceDetail]);

  const setters: Readonly<Record<EditField, (fn: (b: string) => string) => void>> = {
    scopePrefix: (fn) => setScopePrefix((b) => fn(b)),
    addGrant: (fn) => setAddGrant((b) => fn(b)),
    removeGrant: (fn) => setRemoveGrant((b) => fn(b)),
    readonlyPath: (fn) => setReadonlyPath((b) => fn(b)),
  };

  const handleSave = useCallback(() => {
    if (!client || !namespaceDetail) return;

    const update: {
      scope_prefix?: string;
      add_grants?: readonly string[];
      remove_grants?: readonly string[];
      readonly_paths?: readonly string[];
    } = {};

    // Always send scope_prefix (may have changed)
    const newPrefix = scopePrefix.trim() || undefined;
    if (newPrefix !== (namespaceDetail.scope_prefix ?? undefined)) {
      update.scope_prefix = newPrefix ?? "";
    }

    // Add new grant path if entered
    if (addGrant.trim()) {
      update.add_grants = [...namespaceDetail.added_grants, addGrant.trim()];
    }

    // Add new remove grant path if entered
    if (removeGrant.trim()) {
      update.remove_grants = [...namespaceDetail.removed_grants, removeGrant.trim()];
    }

    // Add new readonly path if entered
    if (readonlyPath.trim()) {
      update.readonly_paths = [...namespaceDetail.readonly_paths, readonlyPath.trim()];
    }

    if (Object.keys(update).length > 0) {
      updateNamespaceConfig(delegationId, update, client);
    }

    setAddGrant("");
    setRemoveGrant("");
    setReadonlyPath("");
    setEditing(false);
  }, [client, delegationId, namespaceDetail, scopePrefix, addGrant, removeGrant, readonlyPath, updateNamespaceConfig]);

  const handleUnhandledKey = useCallback(
    (keyName: string) => {
      if (!editing) return;
      const setter = setters[activeField];
      if (keyName.length === 1) {
        setter((b) => b + keyName);
      } else if (keyName === "space") {
        setter((b) => b + " ");
      }
    },
    [editing, activeField],
  );

  useKeyboard(
    {
      escape: () => {
        if (editing) {
          setEditing(false);
        } else {
          onClose();
        }
      },
      e: () => {
        if (!editing && namespaceDetail) {
          setEditing(true);
          setActiveField("scopePrefix");
        }
      },
      return: () => {
        if (editing) {
          handleSave();
        }
      },
      backspace: () => {
        if (editing) {
          setters[activeField]((b) => b.slice(0, -1));
        }
      },
      tab: () => {
        if (editing) {
          const currentIdx = EDIT_FIELD_ORDER.indexOf(activeField);
          const nextIdx = (currentIdx + 1) % EDIT_FIELD_ORDER.length;
          const next = EDIT_FIELD_ORDER[nextIdx];
          if (next) {
            setActiveField(next);
          }
        }
      },
    },
    handleUnhandledKey,
  );

  const ns = namespaceDetail;
  const cursor = "\u2588";

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>{`--- Namespace Config${editing ? " [EDITING]" : ""}: ${delegationId} ---`}</text>
      </box>

      {namespaceDetailLoading && (
        <box height={1} width="100%">
          <text>Loading namespace details...</text>
        </box>
      )}

      {error && !namespaceDetailLoading && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {ns && !namespaceDetailLoading && (
        <>
          <box height={1} width="100%">
            <text>{`  Agent:    ${ns.agent_id}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`  Mode:     ${ns.delegation_mode} (immutable)`}</text>
          </box>

          {/* Scope prefix — editable */}
          {editing ? (
            <box height={1} width="100%">
              <text>
                {activeField === "scopePrefix"
                  ? `> Prefix:   ${scopePrefix}${cursor}`
                  : `  Prefix:   ${scopePrefix}`}
              </text>
            </box>
          ) : (
            <box height={1} width="100%">
              <text>{`  Prefix:   ${ns.scope_prefix ?? "(none)"}`}</text>
            </box>
          )}

          <box height={1} width="100%">
            <text>{`  Zone:     ${ns.zone_id ?? "(none)"}`}</text>
          </box>

          {/* Removed grants */}
          <box height={1} width="100%">
            <text>{`  Removed grants (${ns.removed_grants.length}):`}</text>
          </box>
          {ns.removed_grants.map((g, i) => (
            <box key={`rg-${i}`} height={1} width="100%">
              <text>{`    - ${g}`}</text>
            </box>
          ))}
          {editing && (
            <box height={1} width="100%">
              <text>
                {activeField === "removeGrant"
                  ? `> + remove: ${removeGrant}${cursor}`
                  : `  + remove: ${removeGrant}`}
              </text>
            </box>
          )}

          {/* Added grants */}
          <box height={1} width="100%">
            <text>{`  Added grants (${ns.added_grants.length}):`}</text>
          </box>
          {ns.added_grants.map((g, i) => (
            <box key={`ag-${i}`} height={1} width="100%">
              <text>{`    + ${g}`}</text>
            </box>
          ))}
          {editing && (
            <box height={1} width="100%">
              <text>
                {activeField === "addGrant"
                  ? `> + add:    ${addGrant}${cursor}`
                  : `  + add:    ${addGrant}`}
              </text>
            </box>
          )}

          {/* Readonly paths */}
          <box height={1} width="100%">
            <text>{`  Read-only paths (${ns.readonly_paths.length}):`}</text>
          </box>
          {ns.readonly_paths.map((p, i) => (
            <box key={`ro-${i}`} height={1} width="100%">
              <text>{`    [RO] ${p}`}</text>
            </box>
          ))}
          {editing && (
            <box height={1} width="100%">
              <text>
                {activeField === "readonlyPath"
                  ? `> + readonly: ${readonlyPath}${cursor}`
                  : `  + readonly: ${readonlyPath}`}
              </text>
            </box>
          )}

          {/* Mount table */}
          <box height={1} width="100%">
            <text>{`  Mount table (${ns.mount_table.length} entries):`}</text>
          </box>
          {ns.mount_table.length > 0 ? (
            ns.mount_table.map((path, i) => (
              <box key={`mt-${i}`} height={1} width="100%">
                <text>{`    ${path}`}</text>
              </box>
            ))
          ) : (
            <box height={1} width="100%">
              <text>{"    (empty)"}</text>
            </box>
          )}
        </>
      )}

      <box height={1} width="100%">
        <text>
          {editing
            ? "Tab:next field  Enter:save  Escape:cancel edit  Backspace:delete"
            : "e:edit  Escape:close"}
        </text>
      </box>
    </box>
  );
}
