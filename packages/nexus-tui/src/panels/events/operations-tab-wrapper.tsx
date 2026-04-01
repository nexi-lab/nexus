/**
 * Operations tab wrapper: adds keybindings around OperationsTab render.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import React, { useEffect } from "react";
import { useInfraStore } from "../../stores/infra-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { OperationsTab } from "./operations-tab.js";

interface OperationsTabWrapperProps {
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function OperationsTabWrapper({ tabBindings, overlayActive }: OperationsTabWrapperProps): React.ReactNode {
  const client = useApi();

  const operations = useInfraStore((s) => s.operations);
  const operationsLoading = useInfraStore((s) => s.operationsLoading);
  const selectedOperationIndex = useInfraStore((s) => s.selectedOperationIndex);
  const setSelectedOperationIndex = useInfraStore((s) => s.setSelectedOperationIndex);
  const fetchOperations = useInfraStore((s) => s.fetchOperations);

  useEffect(() => {
    if (client) fetchOperations(client);
  }, [client, fetchOperations]);

  const listNav = listNavigationBindings({
    getIndex: () => selectedOperationIndex,
    setIndex: (i) => setSelectedOperationIndex(i),
    getLength: () => operations.length,
  });

  useKeyboard(
    overlayActive
      ? {}
      : {
          ...listNav,
          ...tabBindings,
          r: () => { if (client) fetchOperations(client); },
        },
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box flexGrow={1} width="100%" borderStyle="single">
        <OperationsTab
          operations={operations}
          selectedIndex={selectedOperationIndex}
          loading={operationsLoading}
        />
      </box>
      <box height={1} width="100%">
        <text>{"j/k:navigate  r:refresh  Tab:switch tab"}</text>
      </box>
    </box>
  );
}
