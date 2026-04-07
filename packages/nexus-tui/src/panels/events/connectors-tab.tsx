/**
 * Connectors tab: list view with detail expansion.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import { createSignal, createEffect, Show } from "solid-js";
import type { JSX } from "solid-js";
import { useInfraStore } from "../../stores/infra-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { listNavigationBindings } from "../../shared/hooks/use-list-navigation.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { ConnectorList } from "./connector-list.js";
import { ConnectorDetail } from "./connector-detail.js";

const HELP_LIST = "j/k:navigate  Enter:capabilities  r:refresh  Tab:switch tab";
const HELP_DETAIL = "Escape:back  r:refresh  Tab:switch tab";

interface ConnectorsTabProps {
  readonly tabBindings: Readonly<Record<string, () => void>>;
  readonly overlayActive: boolean;
}

export function ConnectorsTab(props: ConnectorsTabProps): JSX.Element {
  const client = useApi();
  const [detailView, setDetailView] = createSignal(false);

  const connectors = () => useInfraStore((s) => s.connectors);
  const connectorsLoading = () => useInfraStore((s) => s.connectorsLoading);
  const selectedConnectorIndex = () => useInfraStore((s) => s.selectedConnectorIndex);
  const connectorCapabilities = () => useInfraStore((s) => s.connectorCapabilities);
  const capabilitiesLoading = () => useInfraStore((s) => s.capabilitiesLoading);
  const setSelectedConnectorIndex = useInfraStore((s) => s.setSelectedConnectorIndex);
  const fetchConnectors = useInfraStore((s) => s.fetchConnectors);
  const fetchConnectorCapabilities = useInfraStore((s) => s.fetchConnectorCapabilities);

  createEffect(() => {
    if (client) { fetchConnectors(client); setDetailView(false); }
  });

  useKeyboard(
    (): Record<string, () => void> => {
      if (props.overlayActive) return {};
      const listNav = listNavigationBindings({
        getIndex: () => selectedConnectorIndex(),
        setIndex: (i) => setSelectedConnectorIndex(i),
        getLength: () => connectors().length,
      });
      return {
        ...listNav,
        ...props.tabBindings,
        return: () => {
          if (client) {
            const conn = connectors()[selectedConnectorIndex()];
            if (conn) {
              void fetchConnectorCapabilities(conn.name, client);
              setDetailView(true);
            }
          }
        },
        escape: () => {
          if (detailView()) setDetailView(false);
        },
        r: () => {
          if (client) { fetchConnectors(client); setDetailView(false); }
        },
      };
    },
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box flexGrow={1} width="100%" borderStyle="single">
        <Show
          when={detailView() && connectors()[selectedConnectorIndex()]}
          fallback={
            <ConnectorList
              connectors={connectors()}
              selectedIndex={selectedConnectorIndex()}
              loading={connectorsLoading()}
            />
          }
        >
          <ConnectorDetail
            connectorName={connectors()[selectedConnectorIndex()]!.name}
            capabilities={connectorCapabilities()}
            loading={capabilitiesLoading()}
          />
        </Show>
      </box>
      <box height={1} width="100%">
        <text>{detailView() ? HELP_DETAIL : HELP_LIST}</text>
      </box>
    </box>
  );
}
