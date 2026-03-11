/**
 * Payments panel: tabbed layout for Balance and Reservations views.
 *
 * Note: Policies and Audit tabs are deferred — the backend pay surface
 * (pay.py) does not expose /policies or /audit endpoints yet.
 */

import React, { useEffect } from "react";
import { usePaymentsStore } from "../../stores/payments-store.js";
import type { PaymentsTab } from "../../stores/payments-store.js";
import { useKeyboard } from "../../shared/hooks/use-keyboard.js";
import { useApi } from "../../shared/hooks/use-api.js";
import { BalanceCard } from "./balance-card.js";
import { ReservationList } from "./reservation-list.js";

const TAB_ORDER: readonly PaymentsTab[] = [
  "balance",
  "reservations",
];
const TAB_LABELS: Readonly<Record<PaymentsTab, string>> = {
  balance: "Balance",
  reservations: "Reservations",
};

export default function PaymentsPanel(): React.ReactNode {
  const client = useApi();

  const balance = usePaymentsStore((s) => s.balance);
  const balanceLoading = usePaymentsStore((s) => s.balanceLoading);
  const reservations = usePaymentsStore((s) => s.reservations);
  const selectedReservationIndex = usePaymentsStore((s) => s.selectedReservationIndex);
  const reservationsLoading = usePaymentsStore((s) => s.reservationsLoading);
  const activeTab = usePaymentsStore((s) => s.activeTab);
  const error = usePaymentsStore((s) => s.error);

  const fetchBalance = usePaymentsStore((s) => s.fetchBalance);
  const commitReservation = usePaymentsStore((s) => s.commitReservation);
  const releaseReservation = usePaymentsStore((s) => s.releaseReservation);
  const setActiveTab = usePaymentsStore((s) => s.setActiveTab);
  const setSelectedReservationIndex = usePaymentsStore(
    (s) => s.setSelectedReservationIndex,
  );

  // Refresh current view based on active tab.
  // Reservations are tracked locally, so no fetch is needed for that tab.
  const refreshCurrentView = (): void => {
    if (!client) return;

    if (activeTab === "balance") {
      fetchBalance(client);
    }
  };

  // Auto-fetch when tab changes
  useEffect(() => {
    refreshCurrentView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, client]);

  useKeyboard({
    j: () => {
      if (activeTab === "reservations") {
        setSelectedReservationIndex(
          Math.min(selectedReservationIndex + 1, reservations.length - 1),
        );
      }
    },
    down: () => {
      if (activeTab === "reservations") {
        setSelectedReservationIndex(
          Math.min(selectedReservationIndex + 1, reservations.length - 1),
        );
      }
    },
    k: () => {
      if (activeTab === "reservations") {
        setSelectedReservationIndex(Math.max(selectedReservationIndex - 1, 0));
      }
    },
    up: () => {
      if (activeTab === "reservations") {
        setSelectedReservationIndex(Math.max(selectedReservationIndex - 1, 0));
      }
    },
    tab: () => {
      const currentIdx = TAB_ORDER.indexOf(activeTab);
      const nextIdx = (currentIdx + 1) % TAB_ORDER.length;
      const nextTab = TAB_ORDER[nextIdx];
      if (nextTab) {
        setActiveTab(nextTab);
      }
    },
    r: () => refreshCurrentView(),
    c: () => {
      if (activeTab !== "reservations" || !client) return;
      const selected = reservations[selectedReservationIndex];
      if (selected && selected.status === "pending") {
        commitReservation(selected.id, client);
      }
    },
    x: () => {
      if (activeTab !== "reservations" || !client) return;
      const selected = reservations[selectedReservationIndex];
      if (selected && selected.status === "pending") {
        releaseReservation(selected.id, client);
      }
    },
  });

  return (
    <box height="100%" width="100%" flexDirection="column">
      {/* Tab bar */}
      <box height={1} width="100%">
        <text>
          {TAB_ORDER.map((tab) => {
            const label = TAB_LABELS[tab];
            return tab === activeTab ? `[${label}]` : ` ${label} `;
          }).join(" ")}
        </text>
      </box>

      {/* Error display */}
      {error && (
        <box height={1} width="100%">
          <text>{`Error: ${error}`}</text>
        </box>
      )}

      {/* Detail content */}
      <box flexGrow={1} borderStyle="single">
        {activeTab === "balance" && (
          <BalanceCard balance={balance} loading={balanceLoading} />
        )}
        {activeTab === "reservations" && (
          <ReservationList
            reservations={reservations}
            selectedIndex={selectedReservationIndex}
            loading={reservationsLoading}
          />
        )}
      </box>

      {/* Help bar */}
      <box height={1} width="100%">
        <text>
          {"j/k:navigate  Tab:switch tab  r:refresh  c:commit reservation  x:release reservation  q:quit"}
        </text>
      </box>
    </box>
  );
}
