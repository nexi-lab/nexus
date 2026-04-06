/**
 * Audit trail tab: transaction audit log with cursor-based pagination.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import { createSignal, createEffect, onCleanup } from "solid-js";
import type { JSX } from "solid-js";
import { useInfraStore } from "../../stores/infra-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { AuditTrail } from "./audit-trail.js";

interface AuditTabProps {
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function AuditTab(props: AuditTabProps): JSX.Element {
  const client = useApi();
  const [selectedAuditIndex, setSelectedAuditIndex] = createSignal(0);

  const [_rev, _setRev] = createSignal(0);
  const unsub = useInfraStore.subscribe(() => _setRev((r) => r + 1));
  onCleanup(unsub);
  const inf = () => { void _rev(); return useInfraStore.getState(); };

  const auditTransactions = () => inf().auditTransactions;
  const auditLoading = () => inf().auditLoading;
  const auditHasMore = () => inf().auditHasMore;
  const auditNextCursor = () => inf().auditNextCursor;
  const fetchAuditTransactions = useInfraStore.getState().fetchAuditTransactions;

  createEffect(() => {
    if (client) void fetchAuditTransactions({}, client);
  });

  useKeyboard(
    (): Record<string, () => void> => {
      if (props.overlayActive) return {};
      const listNav = listNavigationBindings({
        getIndex: () => selectedAuditIndex(),
        setIndex: (i) => setSelectedAuditIndex(i),
        getLength: () => auditTransactions().length,
      });
      return {
        ...listNav,
        ...props.tabBindings,
        m: () => {
          if (auditHasMore() && auditNextCursor() && client) {
            void fetchAuditTransactions({ cursor: auditNextCursor()! }, client);
          }
        },
        r: () => { if (client) void fetchAuditTransactions({}, client); },
      };
    },
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box flexGrow={1} width="100%" borderStyle="single">
        <AuditTrail
          transactions={auditTransactions()}
          loading={auditLoading()}
          hasMore={auditHasMore()}
          selectedIndex={selectedAuditIndex()}
        />
      </box>
      <box height={1} width="100%">
        <text>{"j/k:navigate  m:load more  r:refresh  Tab:switch tab"}</text>
      </box>
    </box>
  );
}
