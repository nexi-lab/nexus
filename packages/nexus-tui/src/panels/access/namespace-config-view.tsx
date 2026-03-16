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
 * Ctrl+D removes the last item from the focused list.
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

  // Editable local copies of list arrays (populated on edit enter)
  const [editRemovedGrants, setEditRemovedGrants] = useState<readonly string[]>([]);
  const [editAddedGrants, setEditAddedGrants] = useState<readonly string[]>([]);
  const [editReadonlyPaths, setEditReadonlyPaths] = useState<readonly string[]>([]);

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

  const enterEditMode = useCallback(() => {
    if (!namespaceDetail) return;
    setEditing(true);
    setActiveField("scopePrefix");
    setScopePrefix(namespaceDetail.scope_prefix ?? "");
    setEditRemovedGrants([...namespaceDetail.removed_grants]);
    setEditAddedGrants([...namespaceDetail.added_grants]);
    setEditReadonlyPaths([...namespaceDetail.readonly_paths]);
    setAddGrant("");
    setRemoveGrant("");
    setReadonlyPath("");
  }, [namespaceDetail]);

  const setters: Readonly<Record<EditField, (fn: (b: string) => string) => void>> = {
    scopePrefix: (fn) => setScopePrefix((b) => fn(b)),
    addGrant: (fn) => setAddGrant((b) => fn(b)),
    removeGrant: (fn) => setRemoveGrant((b) => fn(b)),
    readonlyPath: (fn) => setReadonlyPath((b) => fn(b)),
  };

  const handleDeleteFromList = useCallback(() => {
    if (!editing) return;
    if (activeField === "removeGrant") {
      setEditRemovedGrants((prev) => prev.slice(0, -1));
    } else if (activeField === "addGrant") {
      setEditAddedGrants((prev) => prev.slice(0, -1));
    } else if (activeField === "readonlyPath") {
      setEditReadonlyPaths((prev) => prev.slice(0, -1));
    }
  }, [editing, activeField]);

  const handleSave = useCallback(() => {
    if (!client || !namespaceDetail) return;

    const update: {
      scope_prefix?: string;
      add_grants?: readonly string[];
      remove_grants?: readonly string[];
      readonly_paths?: readonly string[];
    } = {};

    // scope_prefix: send "" to clear, non-empty to set, omit to leave unchanged
    const newPrefix = scopePrefix.trim();
    const oldPrefix = namespaceDetail.scope_prefix ?? "";
    if (newPrefix !== oldPrefix) {
      update.scope_prefix = newPrefix;
    }

    // Build final arrays: local editable copy + any new text input
    const finalRemoved = removeGrant.trim()
      ? [...editRemovedGrants, removeGrant.trim()]
      : [...editRemovedGrants];
    const finalAdded = addGrant.trim()
      ? [...editAddedGrants, addGrant.trim()]
      : [...editAddedGrants];
    const finalReadonly = readonlyPath.trim()
      ? [...editReadonlyPaths, readonlyPath.trim()]
      : [...editReadonlyPaths];

    // Send full replacement arrays if they differ from server state
    if (JSON.stringify(finalRemoved) !== JSON.stringify(namespaceDetail.removed_grants)) {
      update.remove_grants = finalRemoved;
    }
    if (JSON.stringify(finalAdded) !== JSON.stringify(namespaceDetail.added_grants)) {
      update.add_grants = finalAdded;
    }
    if (JSON.stringify(finalReadonly) !== JSON.stringify(namespaceDetail.readonly_paths)) {
      update.readonly_paths = finalReadonly;
    }

    if (Object.keys(update).length > 0) {
      updateNamespaceConfig(delegationId, update, client);
    }

    setAddGrant("");
    setRemoveGrant("");
    setReadonlyPath("");
    setEditing(false);
  }, [
    client, delegationId, namespaceDetail, scopePrefix,
    addGrant, removeGrant, readonlyPath,
    editRemovedGrants, editAddedGrants, editReadonlyPaths,
    updateNamespaceConfig,
  ]);

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
          enterEditMode();
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
      "ctrl+d": () => {
        handleDeleteFromList();
      },
    },
    handleUnhandledKey,
  );

  const ns = namespaceDetail;
  const cursor = "\u2588";

  // In edit mode, show local editable arrays; in view mode, show server data
  const displayRemovedGrants = editing ? editRemovedGrants : (ns?.removed_grants ?? []);
  const displayAddedGrants = editing ? editAddedGrants : (ns?.added_grants ?? []);
  const displayReadonlyPaths = editing ? editReadonlyPaths : (ns?.readonly_paths ?? []);

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
            <text>{`  Removed grants (${displayRemovedGrants.length}):`}</text>
          </box>
          {displayRemovedGrants.map((g, i) => (
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
            <text>{`  Added grants (${displayAddedGrants.length}):`}</text>
          </box>
          {displayAddedGrants.map((g, i) => (
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
            <text>{`  Read-only paths (${displayReadonlyPaths.length}):`}</text>
          </box>
          {displayReadonlyPaths.map((p, i) => (
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
            ? "Tab:next field  Enter:save  Escape:cancel  Backspace:delete char  Ctrl+D:remove last item"
            : "e:edit  Escape:close"}
        </text>
      </box>
    </box>
  );
}
