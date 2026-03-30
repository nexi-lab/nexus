import type { ConnectionStatus, PanelId } from "../stores/global-store.js";
import type { AccessTab } from "../stores/access-store.js";
import type { AgentTab } from "../stores/agents-store.js";
import type { SearchTab } from "../stores/search-store.js";
import type { ZoneTab } from "../stores/zones-store.js";
import type { WorkflowTab } from "../stores/workflows-store.js";
import type { PaymentsTab } from "../stores/payments-store.js";
import type { EventsPanelTab } from "../stores/infra-store.js";
import {
  ACCESS_TABS,
  AGENT_TABS,
  EVENTS_TABS,
  PANEL_DESCRIPTORS,
  PAYMENTS_TABS,
  SEARCH_TABS,
  WORKFLOW_TABS,
  ZONE_TABS,
  getTabLabel,
} from "./navigation.js";

export interface StatusBreadcrumbState {
  readonly connectionStatus: ConnectionStatus;
  readonly activePanel: PanelId | null | undefined;
  readonly accessTab?: AccessTab | null;
  readonly agentTab?: AgentTab | null;
  readonly paymentsTab?: PaymentsTab | null;
  readonly searchTab?: SearchTab | null;
  readonly workflowTab?: WorkflowTab | null;
  readonly zoneTab?: ZoneTab | null;
  readonly eventsTab?: EventsPanelTab | null;
}

export function deriveStatusBreadcrumb(state: StatusBreadcrumbState): string | null {
  const { activePanel, connectionStatus } = state;
  if (connectionStatus !== "connected" || !activePanel) return null;

  const panelLabel = PANEL_DESCRIPTORS[activePanel]?.breadcrumbLabel;
  if (!panelLabel) return null;

  let subTabLabel: string | null = null;

  switch (activePanel) {
    case "access":
      subTabLabel = getTabLabel(ACCESS_TABS, state.accessTab);
      break;
    case "agents":
      subTabLabel = getTabLabel(AGENT_TABS, state.agentTab);
      break;
    case "payments":
      subTabLabel = getTabLabel(PAYMENTS_TABS, state.paymentsTab);
      break;
    case "infrastructure":
      subTabLabel = getTabLabel(EVENTS_TABS, state.eventsTab);
      break;
    case "search":
      subTabLabel = getTabLabel(SEARCH_TABS, state.searchTab);
      break;
    case "workflows":
      subTabLabel = getTabLabel(WORKFLOW_TABS, state.workflowTab);
      break;
    case "zones":
      subTabLabel = getTabLabel(ZONE_TABS, state.zoneTab);
      break;
    default:
      subTabLabel = null;
      break;
  }

  if (!subTabLabel || subTabLabel === panelLabel) {
    return panelLabel;
  }

  return `${panelLabel} > ${subTabLabel}`;
}
