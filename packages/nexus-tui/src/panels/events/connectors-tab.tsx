/**
 * Connectors tab: list view with detail expansion.
 *
 * Extracted from events-panel.tsx (Issue 2A).
 */

import React, { useState, useEffect } from "react";
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

export function ConnectorsTab({ tabBindings, overlayActive }: ConnectorsTabProps): React.ReactNode {
  const client = useApi();
  const [detailView, setDetailView] = useState(false);

  const connectors = useInfraStore((s) => s.connectors);
  const connectorsLoading = useInfraStore((s) => s.connectorsLoading);
  const selectedConnectorIndex = useInfraStore((s) => s.selectedConnectorIndex);
  const setSelectedConnectorIndex = useInfraStore((s) => s.setSelectedConnectorIndex);
  const connectorCapabilities = useInfraStore((s) => s.connectorCapabilities);
  const capabilitiesLoading = useInfraStore((s) => s.capabilitiesLoading);
  const fetchConnectors = useInfraStore((s) => s.fetchConnectors);
  const fetchConnectorCapabilities = useInfraStore((s) => s.fetchConnectorCapabilities);

  useEffect(() => {
    if (client) { fetchConnectors(client); setDetailView(false); }
  }, [client, fetchConnectors]);

  const listNav = listNavigationBindings({
    getIndex: () => selectedConnectorIndex,
    setIndex: (i) => setSelectedConnectorIndex(i),
    getLength: () => connectors.length,
  });

  useKeyboard(
    overlayActive
      ? {}
      : {
          ...listNav,
          ...tabBindings,
          return: () => {
            if (client) {
              const conn = connectors[selectedConnectorIndex];
              if (conn) {
                void fetchConnectorCapabilities(conn.name, client);
                setDetailView(true);
              }
            }
          },
          escape: () => {
            if (detailView) setDetailView(false);
          },
          r: () => {
            if (client) { fetchConnectors(client); setDetailView(false); }
          },
        },
  );

  return (
    <box height="100%" width="100%" flexDirection="column">
      <box flexGrow={1} width="100%" borderStyle="single">
        {detailView && connectors[selectedConnectorIndex] ? (
          <ConnectorDetail
            connectorName={connectors[selectedConnectorIndex]!.name}
            capabilities={connectorCapabilities}
            loading={capabilitiesLoading}
          />
        ) : (
          <ConnectorList
            connectors={connectors}
            selectedIndex={selectedConnectorIndex}
            loading={connectorsLoading}
          />
        )}
      </box>
      <box height={1} width="100%">
        <text>{detailView ? HELP_DETAIL : HELP_LIST}</text>
      </box>
    </box>
  );
}
