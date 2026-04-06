/**
 * Operations tab wrapper: adds keybindings around OperationsTab render.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import { createSignal, createEffect, onCleanup } from "solid-js";
import type { JSX } from "solid-js";
import { useInfraStore } from "../../stores/infra-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { OperationsTab } from "./operations-tab.js";

interface OperationsTabWrapperProps {
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function OperationsTabWrapper(props: OperationsTabWrapperProps): JSX.Element {
  const client = useApi();

  const [_rev, _setRev] = createSignal(0);
  const unsub = useInfraStore.subscribe(() => _setRev((r) => r + 1));
  onCleanup(unsub);
  const inf = () => { void _rev(); return useInfraStore.getState(); };

  const operations = () => inf().operations;
  const operationsLoading = () => inf().operationsLoading;
  const selectedOperationIndex = () => inf().selectedOperationIndex;
  const setSelectedOperationIndex = useInfraStore.getState().setSelectedOperationIndex;
  const fetchOperations = useInfraStore.getState().fetchOperations;

  createEffect(() => {
    if (client) fetchOperations(client);
  });

  useKeyboard(
    (): Record<string, () => void> => {
      if (props.overlayActive) return {};
      const listNav = listNavigationBindings({
        getIndex: () => selectedOperationIndex(),
        setIndex: (i) => setSelectedOperationIndex(i),
        getLength: () => operations().length,
      });
      return {
        ...listNav,
        ...props.tabBindings,
        r: () => { if (client) fetchOperations(client); },
      };
    },
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box flexGrow={1} width="100%" borderStyle="single">
        <OperationsTab
          operations={operations()}
          selectedIndex={selectedOperationIndex()}
          loading={operationsLoading()}
        />
      </box>
      <box height={1} width="100%">
        <text>{"j/k:navigate  r:refresh  Tab:switch tab"}</text>
      </box>
    </box>
  );
}
