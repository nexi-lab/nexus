/**
 * Namespace config viewer: shows delegation namespace configuration.
 *
 * Fetches namespace detail from the backend endpoint and displays
 * the delegation mode, scope constraints, grant modifications, and
 * current mount table (visible paths).
 *
 * Escape returns to normal mode.
 */

import React, { useEffect } from "react";
import { useAccessStore } from "../../stores/access-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";

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

  useEffect(() => {
    if (client && delegationId) {
      fetchNamespaceDetail(delegationId, client);
    }
  }, [client, delegationId, fetchNamespaceDetail]);

  useKeyboard({ escape: onClose });

  const ns = namespaceDetail;

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box height={1} width="100%">
        <text>{`--- Namespace Config: ${delegationId} ---`}</text>
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
            <text>{`  Mode:     ${ns.delegation_mode}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`  Prefix:   ${ns.scope_prefix ?? "(none)"}`}</text>
          </box>
          <box height={1} width="100%">
            <text>{`  Zone:     ${ns.zone_id ?? "(none)"}`}</text>
          </box>

          {ns.removed_grants.length > 0 && (
            <>
              <box height={1} width="100%">
                <text>{"  Removed grants:"}</text>
              </box>
              {ns.removed_grants.map((g, i) => (
                <box key={`rg-${i}`} height={1} width="100%">
                  <text>{`    - ${g}`}</text>
                </box>
              ))}
            </>
          )}

          {ns.added_grants.length > 0 && (
            <>
              <box height={1} width="100%">
                <text>{"  Added grants:"}</text>
              </box>
              {ns.added_grants.map((g, i) => (
                <box key={`ag-${i}`} height={1} width="100%">
                  <text>{`    + ${g}`}</text>
                </box>
              ))}
            </>
          )}

          {ns.readonly_paths.length > 0 && (
            <>
              <box height={1} width="100%">
                <text>{"  Read-only paths:"}</text>
              </box>
              {ns.readonly_paths.map((p, i) => (
                <box key={`ro-${i}`} height={1} width="100%">
                  <text>{`    [RO] ${p}`}</text>
                </box>
              ))}
            </>
          )}

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
        <text>{"Escape:close"}</text>
      </box>
    </box>
  );
}
