/**
 * Audit trail tab: transaction audit log with cursor-based pagination.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import { createSignal, createEffect } from "solid-js";
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

  const auditTransactions = () => useInfraStore((s) => s.auditTransactions);
  const auditLoading = () => useInfraStore((s) => s.auditLoading);
  const auditHasMore = () => useInfraStore((s) => s.auditHasMore);
  const auditNextCursor = () => useInfraStore((s) => s.auditNextCursor);
  const fetchAuditTransactions = useInfraStore((s) => s.fetchAuditTransactions);

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
